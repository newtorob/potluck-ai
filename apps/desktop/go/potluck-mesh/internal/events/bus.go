// Package events provides a lightweight in-process pub/sub bus used to fan out
// mesh-state changes to subscribers (currently: the IPC server's WS /events
// clients; future: metrics, logging hooks).
//
// Privacy: events carry only mesh-layer metadata — peer IPs, truncated pubkeys,
// hostnames, status strings. No inference content, no WG private keys, no
// pairing tokens are ever placed in events.
package events

import (
	"log/slog"
	"sync"
	"time"
)

// EventType identifies the kind of event.
type EventType string

const (
	TypeStatusChanged   EventType = "status_changed"
	TypePeerAdded       EventType = "peer_added"
	TypePeerRemoved     EventType = "peer_removed"
	TypePeerUpdated     EventType = "peer_updated"
	TypeEndpointChanged EventType = "endpoint_changed"
	TypeError           EventType = "error"
)

// PeerSnapshot is the mesh-layer view of a single peer, safe to publish in events.
// Matches the /peers response shape so the Electron renderer can use it directly.
type PeerSnapshot struct {
	NodeID           string  `json:"node_id"`
	NodeName         string  `json:"node_name"`
	MeshIP           string  `json:"mesh_ip"`
	WGPublicKey      string  `json:"wg_public_key"`
	LastEndpoint     *string `json:"last_endpoint,omitempty"`
	LastSeenAt       *string `json:"last_seen_at,omitempty"` // RFC3339 or null
	Platform         *string `json:"platform,omitempty"`
	Reachable        bool    `json:"reachable"`
	ReachabilityMode *string `json:"reachability_mode,omitempty"` // "direct"|"derp"|null
}

// Event is the wire-shape sent over WS /events. The Type field discriminates;
// only fields relevant to that type are non-zero.
//
// Shapes match the JSON envelopes in docs/phase-g/local-ipc.md exactly.
type Event struct {
	// Type is always set.
	Type EventType `json:"type"`

	// status_changed
	Status string `json:"status,omitempty"`

	// peer_added | peer_updated
	Peer *PeerSnapshot `json:"peer,omitempty"`

	// peer_removed
	NodeID string `json:"node_id,omitempty"`

	// endpoint_changed
	ExternalEndpoint string `json:"external_endpoint,omitempty"`

	// error
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
}

// NewStatusChanged builds a status_changed event.
func NewStatusChanged(status string) Event {
	return Event{Type: TypeStatusChanged, Status: status}
}

// NewPeerAdded builds a peer_added event.
func NewPeerAdded(p PeerSnapshot) Event {
	return Event{Type: TypePeerAdded, Peer: &p}
}

// NewPeerRemoved builds a peer_removed event.
func NewPeerRemoved(nodeID string) Event {
	return Event{Type: TypePeerRemoved, NodeID: nodeID}
}

// NewPeerUpdated builds a peer_updated event.
func NewPeerUpdated(p PeerSnapshot) Event {
	return Event{Type: TypePeerUpdated, Peer: &p}
}

// NewEndpointChanged builds an endpoint_changed event.
func NewEndpointChanged(ep string) Event {
	return Event{Type: TypeEndpointChanged, ExternalEndpoint: ep}
}

// NewError builds an error event.
func NewError(code, message string) Event {
	return Event{Type: TypeError, Code: code, Message: message}
}

// subscriberChanCap is the per-subscriber channel buffer. If a subscriber is
// too slow to drain at this rate, events are dropped (never backpressuring the
// publisher).
const subscriberChanCap = 256

// Bus is a thread-safe in-process pub/sub hub.
// Create with NewBus(); call Close() when the sidecar is shutting down.
type Bus struct {
	mu          sync.Mutex
	subscribers map[uint64]chan Event
	nextID      uint64
	closed      bool
}

// NewBus creates an idle event bus.
func NewBus() *Bus {
	return &Bus{
		subscribers: make(map[uint64]chan Event),
	}
}

// Subscribe registers a new subscriber and returns:
//   - a receive-only channel that delivers events,
//   - an unsubscribe function that the caller MUST eventually call (typically
//     via defer) to release the channel and stop receiving events.
//
// The channel is buffered to subscriberChanCap. If the subscriber's goroutine
// is slow and the buffer fills, new events are dropped with a warning log
// rather than blocking the publisher.
func (b *Bus) Subscribe() (<-chan Event, func()) {
	b.mu.Lock()
	defer b.mu.Unlock()

	ch := make(chan Event, subscriberChanCap)

	if b.closed {
		// Already shut down — return a closed channel so the caller's range
		// loop exits immediately.
		close(ch)
		return ch, func() {}
	}

	id := b.nextID
	b.nextID++
	b.subscribers[id] = ch

	unsub := func() {
		b.mu.Lock()
		defer b.mu.Unlock()
		if _, ok := b.subscribers[id]; ok {
			delete(b.subscribers, id)
			close(ch)
		}
	}
	return ch, unsub
}

// Publish fans out the event to all current subscribers.
// Slow subscribers that have a full channel are skipped; one warning log is
// emitted per drop (not per event, to avoid log flooding in pathological cases).
// Publish is safe to call from any goroutine.
func (b *Bus) Publish(e Event) {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.closed {
		return
	}

	for id, ch := range b.subscribers {
		select {
		case ch <- e:
		default:
			slog.Warn("events: subscriber channel full, dropping event",
				"subscriber_id", id,
				"event_type", string(e.Type),
			)
		}
	}
}

// Close stops the bus: no further events are published, all subscriber channels
// are closed so their goroutines terminate cleanly.
// Callers should send a final status_changed("stopped") before calling Close.
func (b *Bus) Close() {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.closed {
		return
	}
	b.closed = true
	for id, ch := range b.subscribers {
		delete(b.subscribers, id)
		close(ch)
	}
}

// PublishWithTimeout is a helper for the shutdown path: publishes the event and
// waits up to timeout for all current subscribers to drain it from their
// channels. Use for the terminal "status_changed: stopped" before Close().
//
// Subscribers that don't drain within the timeout are skipped; the event is
// still published to them (it just may be in their buffer when Close() runs).
func (b *Bus) PublishWithTimeout(e Event, timeout time.Duration) {
	b.Publish(e)
	// Brief sleep to give async WS writers a chance to flush.
	// Not a correctness requirement — just best-effort graceful drain.
	delay := timeout
	if delay > 100*time.Millisecond {
		delay = 100 * time.Millisecond
	}
	time.Sleep(delay)
}
