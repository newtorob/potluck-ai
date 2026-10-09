package circlelink

import (
	"net/netip"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// request is a circle_peer_add after validation: every field is in a form the
// rest of the controller can use without checking it again.
type request struct {
	linkID string
	role   string
	key    string
	addr   netip.Addr
	port   uint16
	ttl    time.Duration
	token  string // lowercase hex, role lender only

	window time.Duration // how long to try a direct path before the relay
	wan    string        // a public endpoint, or ""
	lans   []string      // private LAN candidates the message supplied
}

// rejection is a reason from the fixture's ack_reason list plus a short fixed
// phrase for the log. The phrase never contains a wire value.
type rejection struct {
	reason string
	why    string
}

func reject(reason, why string) *rejection { return &rejection{reason: reason, why: why} }

// validateShape checks the fields of an add that need no knowledge of this
// host. The address is parsed but not judged here: that needs the host's own
// address and the household, see Controller.judgeAddr.
func validateShape(m signal.CirclePeerAdd) (request, *rejection) {
	var r request
	if m.Role != signal.CircleRoleLender && m.Role != signal.CircleRoleRequester {
		// A role this build has never heard of is a newer protocol, not bad data.
		return r, reject(signal.CircleReasonUnsupported, "unknown role")
	}
	r.linkID, r.role = m.LinkID, m.Role

	// The roster's own key rule, so one definition of a usable key covers both.
	if err := peerstate.ValidateWGKey(m.PeerWGPublicKey); err != nil {
		return r, reject(signal.CircleReasonInvalidPeer, "peer key is not a canonical base64 32-byte key")
	}
	r.key = m.PeerWGPublicKey

	addr, err := netip.ParseAddr(m.PeerMeshIP)
	if err != nil || !addr.Is4() {
		return r, reject(signal.CircleReasonInvalidPeer, "peer mesh address is not a plain IPv4 address")
	}
	r.addr = addr

	if m.Port < 1 || m.Port > 65535 {
		return r, reject(signal.CircleReasonInvalidPeer, "port out of range")
	}
	r.port = uint16(m.Port)

	if m.TTLSeconds < 1 {
		return r, reject(signal.CircleReasonInvalidPeer, "ttl_s must be positive")
	}
	r.ttl = clampTTL(m.TTLSeconds)

	switch {
	case m.Role == signal.CircleRoleLender:
		if m.TokenSHA256 == nil || !validTokenHash(*m.TokenSHA256) {
			return r, reject(signal.CircleReasonInvalidPeer, "a lender add needs a token hash")
		}
		r.token = *m.TokenSHA256
	case m.TokenSHA256 != nil:
		return r, reject(signal.CircleReasonInvalidPeer, "a requester add carries no token hash")
	}

	if m.DirectWindowMS < 0 {
		return r, reject(signal.CircleReasonInvalidPeer, "direct_window_ms is negative")
	}
	r.window = time.Duration(m.DirectWindowMS) * time.Millisecond
	if r.window > MaxDirectWindow {
		r.window = MaxDirectWindow
	}

	// Endpoints are best effort. One that fails the check is dropped and the
	// link then goes to the relay; it never fails the add.
	if m.PeerEndpoint != nil {
		if ep, ok := publicEndpoint(*m.PeerEndpoint); ok {
			r.wan = ep
		}
	}
	for _, raw := range m.PeerLocalEndpoints {
		if ep, ok := lanEndpoint(raw); ok && len(r.lans) < maxLocalEndpoints {
			r.lans = append(r.lans, ep)
		}
	}
	return r, nil
}

// clampTTL turns ttl_s into a duration, at most MaxTTL.
func clampTTL(seconds int) time.Duration {
	if seconds > int(MaxTTL/time.Second) {
		return MaxTTL
	}
	return time.Duration(seconds) * time.Second
}

// validTokenHash reports whether s is 64 lowercase hex characters, the shape the
// coordinator stores and the engine compares against.
func validTokenHash(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return true
}

// publicEndpoint accepts "ip:port" only for a public unicast address. The
// coordinator forwards the other member's endpoint, which that member chose, so
// the check is what keeps a hostile value from making this host send UDP at its
// own LAN, at loopback, or at an address inside the mesh (which the host routes
// into the tunnel). The reply form is canonical.
func publicEndpoint(raw string) (string, bool) {
	ap, err := netip.ParseAddrPort(raw)
	if err != nil || ap.Port() == 0 {
		return "", false
	}
	a := ap.Addr()
	if a.Zone() != "" {
		return "", false
	}
	a = a.Unmap()
	if !a.IsGlobalUnicast() || a.IsPrivate() {
		return "", false
	}
	if a.Is4() {
		b := a.As4()
		switch {
		case b[0] == 0: // "this network"
			return "", false
		case b[0] >= 240: // reserved
			return "", false
		case b[0] == 100 && b[1]&0xc0 == 64: // shared address space: the mesh pool itself
			return "", false
		}
	}
	return netip.AddrPortFrom(a, ap.Port()).String(), true
}

// lanEndpoint accepts "ip:port" only for an RFC1918 IPv4 address, the shape the
// coordinator allows in peer_local_endpoints.
func lanEndpoint(raw string) (string, bool) {
	ap, err := netip.ParseAddrPort(raw)
	if err != nil || ap.Port() == 0 {
		return "", false
	}
	a := ap.Addr()
	if !a.Is4() || !a.IsPrivate() {
		return "", false
	}
	return ap.String(), true
}
