package signal

// Direct circle links: the three messages the coordinator sends to set up,
// extend and drop a restricted WireGuard peer in another household, and the ack
// this sidecar answers with. Wire shapes mirror schemas/mesh.py
// (SignalCirclePeerAdd, SignalCirclePeerExtend, SignalCirclePeerRemove,
// SignalCirclePeerAck) and are pinned to
// services/coordinator/tests/fixtures/circle_link_shapes.json by a test.
//
// Privacy: none of these carry a member id, node id, node name, hostname or
// circle name. The peer is named only by what the tunnel needs.

import (
	"encoding/json"
	"log/slog"
	"time"
)

// FeatureCircleLinkV1 is the name a sidecar puts in hello.features to say it
// understands the circle_peer_* messages and serves GET /circle-peers.
const FeatureCircleLinkV1 = "circle_link_v1"

// SidecarFeatures returns the features this build advertises, in hello and in
// GET /status. A fresh slice every call.
func SidecarFeatures() []string { return []string{FeatureCircleLinkV1} }

// Roles in circle_peer_add. The role is the RECEIVING sidecar's role.
const (
	CircleRoleLender    = "lender"
	CircleRoleRequester = "requester"
)

// Reasons in circle_peer_remove.
const (
	CircleRemoveEnded      = "ended"
	CircleRemoveRevoked    = "revoked"
	CircleRemoveExpired    = "expired"
	CircleRemoveSuperseded = "superseded"
)

// Ops and states in circle_peer_ack.
const (
	CircleOpAdd    = "add"
	CircleOpExtend = "extend"

	CircleStateReady    = "ready"
	CircleStateRejected = "rejected"
)

// Reasons in a rejected circle_peer_ack.
const (
	CircleReasonInvalidPeer     = "invalid_peer"
	CircleReasonAddressConflict = "address_conflict"
	CircleReasonLimit           = "limit"
	CircleReasonUnsupported     = "unsupported"
	CircleReasonNotEnrolled     = "not_enrolled"
	CircleReasonTunnelError     = "tunnel_error"
	CircleReasonUnknownGrant    = "unknown_grant"
)

// CirclePeerAdd asks this sidecar to add the other machine of a link as a
// restricted peer. Port is the lender engine's circle port: for role lender it
// is a port on this host, for role requester it is a port on the peer.
type CirclePeerAdd struct {
	Type               string   `json:"type"`
	LinkID             string   `json:"link_id"`
	Role               string   `json:"role"`
	PeerWGPublicKey    string   `json:"peer_wg_public_key"`
	PeerMeshIP         string   `json:"peer_mesh_ip"`
	Port               int      `json:"port"`
	TTLSeconds         int      `json:"ttl_s"`
	TokenSHA256        *string  `json:"token_sha256"`
	PeerEndpoint       *string  `json:"peer_endpoint"`
	PeerLocalEndpoints []string `json:"peer_local_endpoints"`
	DirectWindowMS     int      `json:"direct_window_ms"`
}

// CirclePeerExtend keeps a held link TTLSeconds longer, counted from receipt.
type CirclePeerExtend struct {
	Type         string  `json:"type"`
	LinkID       string  `json:"link_id"`
	TTLSeconds   int     `json:"ttl_s"`
	PeerEndpoint *string `json:"peer_endpoint"`
}

// CirclePeerRemove drops a link now. It is idempotent and has no ack.
type CirclePeerRemove struct {
	Type   string `json:"type"`
	LinkID string `json:"link_id"`
	Reason string `json:"reason"`
}

// CirclePeerAck is this sidecar's answer to an add or an extend. Reason is null
// for ready.
type CirclePeerAck struct {
	Type   string  `json:"type"`
	LinkID string  `json:"link_id"`
	Op     string  `json:"op"`
	State  string  `json:"state"`
	Reason *string `json:"reason"`
}

// NewCirclePeerAck builds an ack. reason is ignored for state ready.
func NewCirclePeerAck(linkID, op, state, reason string) CirclePeerAck {
	a := CirclePeerAck{Type: "circle_peer_ack", LinkID: linkID, Op: op, State: state}
	if state == CircleStateRejected && reason != "" {
		a.Reason = &reason
	}
	return a
}

// CircleHandler receives the circle_peer_* messages. It is a separate interface
// from RosterHandler on purpose: adding methods to RosterHandler would break
// every existing implementation, and a sidecar built without circle links simply
// has no CircleHandler (the messages are then ignored, as an old sidecar would).
// The methods run on the goroutine that reads the WebSocket, in the order the
// coordinator sent the messages, and must not block for long.
type CircleHandler interface {
	OnCirclePeerAdd(CirclePeerAdd)
	OnCirclePeerExtend(CirclePeerExtend)
	OnCirclePeerRemove(CirclePeerRemove)
}

// ackSendWait is how long SendCirclePeerAck waits for room in the send buffer.
// The coordinator waits two seconds for an ack and revokes the link without one,
// so a dropped ack costs a whole link; the wait is short because the caller is
// the read loop.
const ackSendWait = 250 * time.Millisecond

// SetCircleHandler installs the handler for circle_peer_* messages. Call it
// before Run. Without one the messages are ignored.
func (c *Client) SetCircleHandler(h CircleHandler) {
	c.circleMu.Lock()
	c.circle = h
	c.circleMu.Unlock()
}

func (c *Client) circleHandler() CircleHandler {
	c.circleMu.RLock()
	defer c.circleMu.RUnlock()
	return c.circle
}

// SetFeatures sets the features the next hello advertises. It does not change a
// connection that is already open.
func (c *Client) SetFeatures(features []string) {
	cp := append([]string(nil), features...)
	c.circleMu.Lock()
	c.features = cp
	c.circleMu.Unlock()
}

func (c *Client) helloFeatures() []string {
	c.circleMu.RLock()
	defer c.circleMu.RUnlock()
	return append([]string(nil), c.features...)
}

// SendCirclePeerAck queues an ack on the active connection. With no connection
// the ack is dropped: the coordinator re-sends the add when the sidecar
// reconnects, and the controller answers a link it already holds with a fresh
// ack.
func (c *Client) SendCirclePeerAck(ack CirclePeerAck) {
	ch := c.loadSendCh()
	if ch == nil {
		slog.Debug("signal: no active connection, dropping circle_peer_ack", "link_id", ack.LinkID)
		return
	}
	select {
	case ch <- ack:
		return
	default:
	}
	t := time.NewTimer(ackSendWait)
	defer t.Stop()
	select {
	case ch <- ack:
	case <-t.C:
		slog.Warn("signal: send channel full, dropping circle_peer_ack", "link_id", ack.LinkID)
	}
}

// dispatchCircle handles one circle_peer_* message from the read loop. A message
// that does not decode is answered with a rejected ack when its link_id can be
// read, so the coordinator does not wait out its timeout.
func (c *Client) dispatchCircle(typ string, raw json.RawMessage) {
	h := c.circleHandler()
	if h == nil {
		slog.Debug("signal: circle message ignored, no handler", "type", typ)
		return
	}
	switch typ {
	case "circle_peer_add":
		var m CirclePeerAdd
		if err := json.Unmarshal(raw, &m); err != nil {
			slog.Warn("signal: decode circle_peer_add failed", "err", err)
			c.rejectUndecodable(raw, CircleOpAdd)
			return
		}
		h.OnCirclePeerAdd(m)
	case "circle_peer_extend":
		var m CirclePeerExtend
		if err := json.Unmarshal(raw, &m); err != nil {
			slog.Warn("signal: decode circle_peer_extend failed", "err", err)
			c.rejectUndecodable(raw, CircleOpExtend)
			return
		}
		h.OnCirclePeerExtend(m)
	case "circle_peer_remove":
		var m CirclePeerRemove
		if err := json.Unmarshal(raw, &m); err != nil {
			slog.Warn("signal: decode circle_peer_remove failed", "err", err)
			return
		}
		h.OnCirclePeerRemove(m)
	}
}

// rejectUndecodable answers a circle_peer_add or _extend whose fields did not
// decode, if a plausible link_id can still be read from it.
func (c *Client) rejectUndecodable(raw json.RawMessage, op string) {
	var id struct {
		LinkID string `json:"link_id"`
	}
	if err := json.Unmarshal(raw, &id); err != nil || !ValidLinkID(id.LinkID) {
		return
	}
	c.SendCirclePeerAck(NewCirclePeerAck(id.LinkID, op, CircleStateRejected, CircleReasonInvalidPeer))
}

// ValidLinkID reports whether s is a canonical lowercase UUID, the only shape
// the coordinator sends. Anything else is never echoed into a log or a reply.
func ValidLinkID(s string) bool {
	if len(s) != 36 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
				return false
			}
		}
	}
	return true
}
