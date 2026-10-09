package circlelink

import (
	"net/netip"
	"strings"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// FuzzAdd throws arbitrary field values at the add path. Whatever comes in, the
// controller must not panic, and an add it acknowledges ready must have installed
// a peer on a usable mesh address that is neither this node's, a household
// machine's, nor outside the mesh pool, with an endpoint (if any) that is a
// public unicast address.
func FuzzAdd(f *testing.F) {
	f.Add(linkA, "lender", keyN(1), circIP1, 8347, 600, tokenA, wanEP, "192.168.1.2:51820", 3000)
	f.Add(linkA, "requester", keyN(2), "100.64.1.2\nallowed_ip=0.0.0.0/0", 1, 5, "", "", "", 0)
	f.Add("x", "lender", "AAAA", "::ffff:100.64.1.2", 0, 0, "zz", "[::1]:80", "10.0.0.1:1", -1)
	f.Add(linkA, "lender", keyN(0x0B), hhIP, 65535, 99999, tokenA, "100.64.0.7:51820", "", 10000)
	f.Add(linkB, "requester", keyN(3), "100.64.0.99", 22, 1800, "", "203.0.113.9:51820\nx", "", 5000)
	f.Fuzz(func(t *testing.T, link, role, key, ip string, port, ttl int, token, ep, lan string, window int) {
		e := newEnv(t)
		m := signal.CirclePeerAdd{Type: "circle_peer_add", LinkID: link, Role: role, PeerWGPublicKey: key, PeerMeshIP: ip,
			Port: port, TTLSeconds: ttl, DirectWindowMS: window}
		if token != "" {
			m.TokenSHA256 = &token
		}
		if ep != "" {
			m.PeerEndpoint = &ep
		}
		if lan != "" {
			m.PeerLocalEndpoints = []string{lan}
		}
		e.c.OnCirclePeerAdd(m)
		ack := e.sig.last()
		if e.sig.count() == 0 {
			return // not answered: the link id was unusable
		}
		if ack.State != signal.CircleStateReady {
			if e.flt.count() != 0 || len(e.tun.peers) != 0 || len(e.tun.routes) != 0 {
				t.Fatalf("a refused add left state behind: %s", e.log)
			}
			return
		}
		a, err := netip.ParseAddr(ip)
		if err != nil || !a.Is4() || !netip.MustParsePrefix("100.64.0.0/10").Contains(a) {
			t.Fatalf("accepted a peer address %q", ip)
		}
		if ip == selfIP || ip == hhIP || strings.HasPrefix(ip, "100.64.0.") {
			t.Fatalf("accepted %q, which is this node's, a household machine's or inside this household's range", ip)
		}
		for _, p := range e.tun.peers {
			if p.Endpoint == "" || strings.HasPrefix(p.Endpoint, "derp:") {
				continue
			}
			ap, err := netip.ParseAddrPort(p.Endpoint)
			if err != nil || !ap.Addr().IsGlobalUnicast() || ap.Addr().IsPrivate() || ap.Addr().IsLoopback() {
				t.Fatalf("configured the endpoint %q", p.Endpoint)
			}
		}
		if g, ok := e.flt.entry(ip); !ok || (g.InPort == 0 && g.OutPort == 0) || g.TTL <= 0 || g.TTL > MaxTTL {
			t.Fatalf("filter entry for an accepted add = %+v, %v", g, ok)
		}
	})
}

// FuzzEndpoints: an endpoint publicEndpoint accepts is a canonical public
// unicast address and port; one lanEndpoint accepts is an RFC1918 IPv4 address.
// Nothing either returns contains a character that could start a new UAPI line.
func FuzzEndpoints(f *testing.F) {
	for _, s := range []string{wanEP, "192.168.1.2:51820", "[2001:db8::1]:51820", "100.64.0.7:1", "1.2.3.4:5\nallowed_ip=0.0.0.0/0", "[::ffff:10.0.0.1]:80", ""} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, raw string) {
		if ep, ok := publicEndpoint(raw); ok {
			ap, err := netip.ParseAddrPort(ep)
			if err != nil || ap.Port() == 0 || strings.ContainsAny(ep, "\n\r \t=") {
				t.Fatalf("publicEndpoint(%q) = %q", raw, ep)
			}
			a := ap.Addr()
			if !a.IsGlobalUnicast() || a.IsPrivate() || a.IsLoopback() || a.IsLinkLocalUnicast() || a.IsMulticast() || a.Zone() != "" {
				t.Fatalf("publicEndpoint(%q) accepted a non-public address %q", raw, ep)
			}
			if a.Is4() && (a.As4()[0] == 0 || a.As4()[0] >= 240 || (a.As4()[0] == 100 && a.As4()[1]&0xc0 == 64)) {
				t.Fatalf("publicEndpoint(%q) accepted a reserved address %q", raw, ep)
			}
		}
		if ep, ok := lanEndpoint(raw); ok {
			ap, err := netip.ParseAddrPort(ep)
			if err != nil || !ap.Addr().Is4() || !ap.Addr().IsPrivate() || strings.ContainsAny(ep, "\n\r \t=") {
				t.Fatalf("lanEndpoint(%q) = %q", raw, ep)
			}
		}
	})
}
