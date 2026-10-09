package localapi

import (
	"net/http"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/circlelink"
)

// CircleSource is what GET /circle-peers and POST /unpair need of the circle
// controller. *circlelink.Controller satisfies it.
type CircleSource interface {
	Peers() []circlelink.PeerView
	Clear()
}

// scopeCircle marks a peer that belongs to someone else, lent for one link. It
// is the opposite of scopeHousehold: a consumer that wants only the owner's own
// machines reads GET /peers, which never lists one of these.
const scopeCircle = "circle"

// circleInbound is a peer that may call a port on this host (this host lends).
// token_sha256 is what the lender engine checks the Bearer token against.
type circleInbound struct {
	LinkID      string `json:"link_id"`
	Port        uint16 `json:"port"`
	TokenSHA256 string `json:"token_sha256"`
	ExpiresInS  int    `json:"expires_in_s"`
}

// circleOutbound is a peer whose port this host may call (this host borrows).
type circleOutbound struct {
	LinkID     string `json:"link_id"`
	Port       uint16 `json:"port"`
	ExpiresInS int    `json:"expires_in_s"`
}

// circlePeerResponse deliberately has no node name, node id, endpoint, local
// endpoints or WireGuard key: a UI must never show a stranger's address, and the
// engine needs only the mesh address, the reachability and the two grants.
type circlePeerResponse struct {
	Scope               string          `json:"scope"`
	MeshIP              string          `json:"mesh_ip"`
	Reachable           bool            `json:"reachable"`
	ReachabilityMode    *string         `json:"reachability_mode"`
	HandshakeAgeSeconds *float64        `json:"handshake_age_seconds"`
	Inbound             *circleInbound  `json:"inbound"`
	Outbound            *circleOutbound `json:"outbound"`
}

type circlePeersResponse struct {
	Peers []circlePeerResponse `json:"peers"`
}

func (s *Server) handleCirclePeers(w http.ResponseWriter, r *http.Request) {
	out := circlePeersResponse{Peers: []circlePeerResponse{}}
	if s.circle != nil {
		for _, v := range s.circle.Peers() {
			out.Peers = append(out.Peers, s.circlePeerResponse(v))
		}
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) circlePeerResponse(v circlelink.PeerView) circlePeerResponse {
	pr := circlePeerResponse{Scope: scopeCircle, MeshIP: v.MeshIP}
	if v.Inbound != nil {
		pr.Inbound = &circleInbound{LinkID: v.Inbound.LinkID, Port: v.Inbound.Port, TokenSHA256: v.Inbound.TokenSHA256, ExpiresInS: v.Inbound.ExpiresInS}
	}
	if v.Outbound != nil {
		pr.Outbound = &circleOutbound{LinkID: v.Outbound.LinkID, Port: v.Outbound.Port, ExpiresInS: v.Outbound.ExpiresInS}
	}
	// The same rule as GET /peers: a handshake no older than three minutes.
	if s.peerReachFn != nil {
		if lastHS, _, _, err := s.peerReachFn(v.PubKey); err == nil && !lastHS.IsZero() {
			age := time.Since(lastHS).Seconds()
			pr.HandshakeAgeSeconds = &age
			pr.Reachable = time.Since(lastHS) < 3*time.Minute
		}
	}
	// reachability_mode: what the link is using once it works, "connecting"
	// while it is being set up, and null when there is nothing to connect with
	// (no endpoint and no relay), so a consumer can stop waiting.
	var mode string
	switch {
	case pr.Reachable && v.Transport == "derp":
		mode = string(ReachDERP)
	case pr.Reachable:
		mode = string(ReachDirect)
	case v.Transport == "none":
		// stays null
	default:
		mode = string(ReachConnecting)
	}
	if mode != "" {
		pr.ReachabilityMode = &mode
	}
	return pr
}
