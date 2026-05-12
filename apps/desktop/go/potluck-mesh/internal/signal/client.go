package signal

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"
)

const (
	// pingInterval is how often we send a control-plane ping to the coordinator.
	// Per design doc: 30s when active. (5min idle and network-transition immediate
	// ping logic are deferred to later weeks when the app-state signal exists.)
	pingInterval = 30 * time.Second

	// maxBackoff caps the reconnect delay to avoid long waits.
	maxBackoff = 60 * time.Second

	// initialBackoff is the first reconnect delay.
	initialBackoff = 500 * time.Millisecond

	// helloTimeout is how long we wait for the welcome after sending hello.
	helloTimeout = 15 * time.Second

	// sendChanCap is the capacity of the outbound message channel.
	// Deep enough to absorb bursts (e.g. endpoint_update arriving while a
	// ping is queued) without blocking callers.
	sendChanCap = 16
)

// RosterHandler is called (on the goroutine that reads from the WS) whenever
// the roster changes or a hole-punch is coordinated. Implementations must not
// block; dispatch to a channel or goroutine if work is needed.
type RosterHandler interface {
	// OnWelcome is called once, immediately after the first successful connect.
	OnWelcome(w Welcome)
	// OnRosterUpdate is called whenever the coordinator broadcasts a change.
	OnRosterUpdate(u RosterUpdate)
	// OnPunchCoord is called when the coordinator delivers a punch_coord message.
	// The coord carries the peer's endpoint and a role discriminator
	// ("initiator" = we asked; "responder" = the peer asked for us).
	OnPunchCoord(coord PunchCoord)
}

// Client manages a persistent WebSocket connection to /mesh/signal.
// It reconnects automatically with exponential backoff on any failure.
type Client struct {
	signalURL   string
	wgPublicKey string
	handler     RosterHandler

	// sendCh is the channel used to enqueue outbound messages from any
	// goroutine. A dedicated writer goroutine drains it, ensuring only
	// one goroutine calls wsjson.Write at a time.
	// The channel is replaced on each new connection inside runOnce.
	sendCh chan any
}

// NewClient creates a Client. Call Run(ctx) to start the connection loop.
func NewClient(signalURL, wgPublicKey string, handler RosterHandler) *Client {
	return &Client{
		signalURL:   signalURL,
		wgPublicKey: wgPublicKey,
		handler:     handler,
	}
}

// SendEndpointUpdate enqueues an endpoint_update message to be sent over the
// active signaling connection. It is safe to call from any goroutine. If there
// is no active connection (e.g. during reconnect backoff), the message is
// dropped with a debug log — the next STUN refresh will retry.
func (c *Client) SendEndpointUpdate(endpoint string) {
	ch := c.sendCh // read once, atomically — nil if no connection yet
	if ch == nil {
		slog.Debug("signal: no active connection, dropping endpoint_update",
			"endpoint", endpoint,
		)
		return
	}
	select {
	case ch <- NewEndpointUpdate(endpoint):
		slog.Debug("signal: queued endpoint_update", "endpoint", endpoint)
	default:
		// Channel full — highly unlikely given the cap, but never block.
		slog.Warn("signal: send channel full, dropping endpoint_update",
			"endpoint", endpoint,
		)
	}
}

// SendRequestPunch enqueues a request_punch message asking the coordinator to
// broker a simultaneous-send hole-punch with the named peer. It is safe to
// call from any goroutine. If there is no active connection the message is
// dropped — the caller (main.go punch throttle) will retry on the next roster
// event or after its 30-second cooldown expires.
func (c *Client) SendRequestPunch(targetNodeID string) {
	ch := c.sendCh
	if ch == nil {
		slog.Debug("signal: no active connection, dropping request_punch",
			"target_node_id", targetNodeID,
		)
		return
	}
	select {
	case ch <- NewRequestPunch(targetNodeID):
		slog.Debug("signal: queued request_punch", "target_node_id", targetNodeID)
	default:
		slog.Warn("signal: send channel full, dropping request_punch",
			"target_node_id", targetNodeID,
		)
	}
}

// Run connects to the coordinator's /mesh/signal endpoint and maintains the
// connection until ctx is cancelled. Reconnects with exponential backoff on
// any disconnect or error. The backoff resets to initialBackoff after a
// successful welcome exchange (connection that got past hello is "established").
//
// Blocks until ctx is cancelled. Returns ctx.Err() when done.
func (c *Client) Run(ctx context.Context) error {
	backoff := initialBackoff
	attempt := 0

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		attempt++
		slog.Info("signal: connecting", "url", c.signalURL, "attempt", attempt)

		established, err := c.runOnce(ctx)
		// Clear the send channel so callers don't try to write to a closed conn.
		c.sendCh = nil

		if ctx.Err() != nil {
			// Normal shutdown — parent context cancelled.
			return ctx.Err()
		}

		if established {
			// Connection got past hello/welcome — reset backoff so the
			// next reconnect doesn't wait if this was a transient drop.
			backoff = initialBackoff
		}

		if err != nil {
			slog.Warn("signal: disconnected — will reconnect",
				"attempt", attempt,
				"backoff_ms", backoff.Milliseconds(),
				"err", err,
			)
		} else {
			slog.Info("signal: connection closed cleanly — reconnecting",
				"attempt", attempt,
				"backoff_ms", backoff.Milliseconds(),
			)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff):
		}

		// Exponential backoff: 0.5s, 1s, 2s, 4s … capped at maxBackoff.
		backoff = time.Duration(math.Min(float64(backoff*2), float64(maxBackoff)))
	}
}

// runOnce establishes a single connection, drives the message loop, and
// returns when the connection is closed or an unrecoverable error occurs.
// Returns (true, nil-or-err) if a welcome was successfully received (connection
// was established), or (false, err) if the connection never got past hello.
func (c *Client) runOnce(ctx context.Context) (established bool, _ error) {
	dialCtx, dialCancel := context.WithTimeout(ctx, 15*time.Second)
	defer dialCancel()

	conn, _, err := websocket.Dial(dialCtx, c.signalURL, nil)
	if err != nil {
		return false, fmt.Errorf("signal: dial: %w", err)
	}
	defer conn.CloseNow() //nolint:errcheck

	// ---- send hello ---- //
	if err := wsjson.Write(ctx, conn, NewHello(c.wgPublicKey)); err != nil {
		return false, fmt.Errorf("signal: send hello: %w", err)
	}

	// ---- wait for welcome ---- //
	welcomeCtx, welcomeCancel := context.WithTimeout(ctx, helloTimeout)
	defer welcomeCancel()

	var rawWelcome json.RawMessage
	if err := wsjson.Read(welcomeCtx, conn, &rawWelcome); err != nil {
		return false, fmt.Errorf("signal: read welcome: %w", err)
	}

	var env Envelope
	if err := json.Unmarshal(rawWelcome, &env); err != nil {
		return false, fmt.Errorf("signal: parse envelope: %w", err)
	}
	if env.Type == "error" {
		var se ServerError
		_ = json.Unmarshal(rawWelcome, &se)
		return false, fmt.Errorf("signal: server error on hello: code=%s message=%s", se.Code, se.Message)
	}
	if env.Type != "welcome" {
		return false, fmt.Errorf("signal: expected welcome, got type=%q", env.Type)
	}

	var welcome Welcome
	if err := json.Unmarshal(rawWelcome, &welcome); err != nil {
		return false, fmt.Errorf("signal: decode welcome: %w", err)
	}

	slog.Info("signal: connected",
		"node_id", welcome.NodeID,
		"household_id", welcome.HouseholdID,
		"mesh_ip", welcome.MeshIP,
		"roster_size", len(welcome.Roster),
	)

	// Arm the outbound channel so SendEndpointUpdate can start writing.
	sendCh := make(chan any, sendChanCap)
	c.sendCh = sendCh

	c.handler.OnWelcome(welcome)

	// ---- ping ticker ---- //
	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()

	// ---- message loops ---- //
	// Read loop goroutine.
	readErr := make(chan error, 1)
	go func() {
		readErr <- c.readLoop(ctx, conn)
	}()

	// Write loop goroutine — drains sendCh so all writes happen on one goroutine.
	writeErr := make(chan error, 1)
	go func() {
		writeErr <- c.writeLoop(ctx, conn, sendCh)
	}()

	for {
		select {
		case <-ctx.Done():
			_ = conn.Close(websocket.StatusNormalClosure, "shutdown")
			return true, ctx.Err()

		case <-ticker.C:
			select {
			case sendCh <- NewPing():
			default:
				slog.Warn("signal: send channel full, dropping ping")
			}

		case err := <-readErr:
			if err != nil {
				return true, fmt.Errorf("signal: read loop: %w", err)
			}
			return true, nil

		case err := <-writeErr:
			if err != nil {
				return true, fmt.Errorf("signal: write loop: %w", err)
			}
			return true, nil
		}
	}
}

// writeLoop drains the sendCh and writes each message to the connection.
// Runs on its own goroutine so writes are serialised.
func (c *Client) writeLoop(ctx context.Context, conn *websocket.Conn, sendCh <-chan any) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case msg, ok := <-sendCh:
			if !ok {
				return nil // channel closed — connection is going away
			}
			if err := wsjson.Write(ctx, conn, msg); err != nil {
				return fmt.Errorf("write: %w", err)
			}
		}
	}
}

// readLoop reads messages from the connection until it closes, dispatching
// to the handler for roster events.
func (c *Client) readLoop(ctx context.Context, conn *websocket.Conn) error {
	for {
		var raw json.RawMessage
		if err := wsjson.Read(ctx, conn, &raw); err != nil {
			return err // connection closed or context cancelled
		}

		var env Envelope
		if err := json.Unmarshal(raw, &env); err != nil {
			slog.Warn("signal: received non-JSON message, ignoring")
			continue
		}

		switch env.Type {
		case "pong":
			var p Pong
			if err := json.Unmarshal(raw, &p); err == nil {
				slog.Debug("signal: pong", "server_time", p.ServerTime.Format(time.RFC3339))
			}

		case "roster_update":
			var ru RosterUpdate
			if err := json.Unmarshal(raw, &ru); err != nil {
				slog.Warn("signal: decode roster_update failed", "err", err)
				continue
			}
			logRosterUpdate(ru)
			c.handler.OnRosterUpdate(ru)

		case "punch_coord":
			var pc PunchCoord
			if err := json.Unmarshal(raw, &pc); err != nil {
				slog.Warn("signal: decode punch_coord failed", "err", err)
				continue
			}
			c.handler.OnPunchCoord(pc)

		case "error":
			var se ServerError
			if err := json.Unmarshal(raw, &se); err == nil {
				slog.Warn("signal: server error",
					"code", se.Code,
					"message", se.Message,
				)
			}

		default:
			if !knownType(env.Type) {
				slog.Debug("signal: unknown message type (protocol extension?)", "type", env.Type)
			}
		}
	}
}

// logRosterUpdate logs a summary of roster changes using only mesh-layer
// metadata. Never logs peer content beyond mesh_ip and node_id.
func logRosterUpdate(ru RosterUpdate) {
	// Log counts + mesh_ip/node_id only — nothing privacy-sensitive.
	if len(ru.Added) > 0 {
		added := make([]string, 0, len(ru.Added))
		for _, p := range ru.Added {
			added = append(added, fmt.Sprintf("%s(%s)", p.NodeID, p.MeshIP))
		}
		slog.Info("signal: roster added", "count", len(ru.Added), "nodes", added)
	}
	if len(ru.Updated) > 0 {
		updated := make([]string, 0, len(ru.Updated))
		for _, p := range ru.Updated {
			updated = append(updated, fmt.Sprintf("%s(%s)", p.NodeID, p.MeshIP))
		}
		slog.Info("signal: roster updated", "count", len(ru.Updated), "nodes", updated)
	}
	if len(ru.Removed) > 0 {
		slog.Info("signal: roster removed", "count", len(ru.Removed), "node_ids", ru.Removed)
	}
}
