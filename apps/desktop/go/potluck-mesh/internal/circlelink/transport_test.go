package circlelink

import (
	"strings"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// No direct window: straight to the relay. No packet is ever addressed to a raw
// endpoint, whether or not the message happened to carry one.
func TestRelayOnlyPolicyNeverUsesAnEndpoint(t *testing.T) {
	e := newEnv(t)
	k := keyN(60)
	m := addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600)
	m.PeerEndpoint = sp(wanEP) // not allowed by the contract, but the sidecar must still not use it
	mustReady(t, e.add(m), signal.CircleOpAdd)
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) || p.PersistentKeepalive != 25 {
		t.Errorf("peer = %+v, want the relay endpoint with the standard keepalive", p)
	}
	if !e.rel.routed(k) {
		t.Error("the relay flag is off")
	}
	// Run the clock well past any window and any timer.
	for i := 0; i < 20; i++ {
		e.clk.advance(time.Second)
		e.c.Sweep()
	}
	for _, op := range e.log.list() {
		if strings.Contains(op, "203.0.113.9") {
			t.Fatalf("a packet path to the raw endpoint was configured under the relay-only policy: %s", op)
		}
	}
	if v := e.c.Peers(); v[0].Transport != "derp" {
		t.Errorf("transport = %s", v[0].Transport)
	}
}

// No relay and no window: nothing is sent, the peer is held, and the keepalive is
// off so the device does not log an error every few seconds.
func TestNoRelayAndNoEndpointMeansNothingIsSent(t *testing.T) {
	e := newEnv(t, withoutRelay())
	k := keyN(61)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	if p := e.tun.peer(k); p.Endpoint != "" || p.PersistentKeepalive != 0 {
		t.Errorf("peer = %+v, want no endpoint and no keepalive", p)
	}
	if v := e.c.Peers(); v[0].Transport != "none" {
		t.Errorf("transport = %s", v[0].Transport)
	}
	for i := 0; i < 30; i++ {
		e.clk.advance(time.Second)
		e.c.Sweep()
	}
	if got := strings.Count(e.log.String(), "AddPeer "); got != 1 {
		t.Errorf("the peer was re-added %d times with nothing to do", got)
	}
}

// The direct policy: pin the endpoint, relay flag off, probe keepalive, and try
// for the window.
func TestDirectPolicyPinsTheEndpointFirst(t *testing.T) {
	e := newEnv(t)
	k := keyN(62)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	if p := e.tun.peer(k); p.Endpoint != wanEP || p.PersistentKeepalive != 5 {
		t.Errorf("peer = %+v", p)
	}
	if e.rel.routed(k) {
		t.Error("the relay flag is on during the direct try")
	}
	if !strings.Contains(e.log.String(), "SetPeerRouting 3e false") {
		t.Errorf("the relay flag was not set off explicitly: %s", e.log)
	}
}

// No handshake inside the window: the peer moves to the relay, removed and added
// again so a handshake starts over the relay at once.
func TestDirectWindowExpiresToTheRelay(t *testing.T) {
	e := newEnv(t)
	k := keyN(63)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(2900 * time.Millisecond)
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != wanEP {
		t.Fatalf("moved to the relay before the window ended: %+v", p)
	}
	e.clk.advance(200 * time.Millisecond)
	e.log.reset()
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) || p.PersistentKeepalive != 25 {
		t.Fatalf("peer after the window = %+v", p)
	}
	if !e.rel.routed(k) {
		t.Error("the relay flag is off after the fallback")
	}
	if v := e.c.Peers(); v[0].Transport != "derp" {
		t.Errorf("transport = %s", v[0].Transport)
	}
	want := "RemovePeer k3f | AddPeer k3f ep=" + synthetic(k) + " ka=25"
	if !strings.Contains(strings.Join(e.log.list(), " | "), want) {
		t.Errorf("fallback operations: %s\nwant to contain: %s", e.log, want)
	}
}

// A handshake inside the window confirms the direct path: it stays off the relay
// and goes to the standard keepalive.
func TestHandshakeInsideTheWindowConfirmsDirect(t *testing.T) {
	e := newEnv(t)
	k := keyN(64)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(800 * time.Millisecond)
	e.tun.setHandshake(k, e.clk.wallTime())
	e.c.Sweep()
	e.clk.advance(5 * time.Second)
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != wanEP || p.PersistentKeepalive != 25 {
		t.Errorf("peer = %+v, want the pinned endpoint at the standard keepalive", p)
	}
	if e.rel.routed(k) {
		t.Error("a confirmed direct path was put on the relay")
	}
	if v := e.c.Peers(); v[0].Transport != "direct" {
		t.Errorf("transport = %s", v[0].Transport)
	}
}

// A handshake that arrives over the relay during the window (the other side fell
// back first and roamed this peer there) makes the peer a relay peer.
func TestHandshakeOverTheRelayDuringTheWindowIsRelayed(t *testing.T) {
	e := newEnv(t)
	k := keyN(65)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(1200 * time.Millisecond)
	e.tun.setHandshake(k, e.clk.wallTime())
	e.tun.liveEP[k] = "derp:" + "00000000"
	e.c.Sweep()
	if v := e.c.Peers(); v[0].Transport != "derp" {
		t.Errorf("transport = %s, want derp", v[0].Transport)
	}
	if !e.rel.routed(k) {
		t.Error("the relay flag is off for a peer reached over the relay")
	}
}

// With no relay to fall back to, a direct try that gets no handshake just goes on.
func TestDirectWithoutARelayStaysDirect(t *testing.T) {
	e := newEnv(t, withoutRelay())
	k := keyN(66)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(4 * time.Second)
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != wanEP || p.PersistentKeepalive != 25 {
		t.Errorf("peer = %+v, want the endpoint kept at the standard keepalive", p)
	}
	if v := e.c.Peers(); v[0].Transport != "direct" {
		t.Errorf("transport = %s", v[0].Transport)
	}
}

// A confirmed direct path is judged dead by the three minute window, not by a
// short one.
func TestConfirmedDirectPathGoesStaleAtThreeMinutes(t *testing.T) {
	e := newEnv(t)
	k := keyN(67)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 1800), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(500 * time.Millisecond)
	hs := e.clk.wallTime()
	e.tun.setHandshake(k, hs)
	e.c.Sweep() // confirmed
	for i := 0; i < 17; i++ {
		e.clk.advance(10 * time.Second) // 170 s since the handshake
		e.c.Sweep()
	}
	if p := e.tun.peer(k); p.Endpoint != wanEP {
		t.Fatalf("judged dead at 170s, inside the three minute window: %+v", p)
	}
	for i := 0; i < 3; i++ {
		e.clk.advance(10 * time.Second) // 200 s
		e.c.Sweep()
	}
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) {
		t.Errorf("not moved to the relay after the handshake went 200s stale: %+v", p)
	}
}

// A symmetric NAT on this node: straight to the relay when there is one; the
// direct try is the only thing left when there is not.
func TestSkipDirectOnSymmetricNAT(t *testing.T) {
	sym := func() bool { return true }
	e := newEnv(t, withSkipDirect(sym))
	k := keyN(68)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) {
		t.Errorf("symmetric NAT with a relay: peer = %+v", p)
	}
	e2 := newEnv(t, withSkipDirect(sym), withoutRelay())
	mustReady(t, e2.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	if p := e2.tun.peer(k); p.Endpoint != wanEP {
		t.Errorf("symmetric NAT without a relay should still try direct: %+v", p)
	}
}

// The first handshake attempt can arrive before the other side has installed the
// peer. A peer with no handshake 1.5s in is re-added once; a peer with one is not.
func TestPokeReAddsOncePeerWithoutAHandshake(t *testing.T) {
	e := newEnv(t)
	k := keyN(69)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.clk.advance(1400 * time.Millisecond)
	e.c.Sweep()
	if strings.Count(e.log.String(), "RemovePeer") != 0 {
		t.Fatal("re-added before 1.5s")
	}
	e.clk.advance(200 * time.Millisecond)
	e.c.Sweep()
	if got := strings.Count(e.log.String(), "RemovePeer k45"); got != 1 {
		t.Errorf("RemovePeer calls at 1.6s = %d, want 1", got)
	}
	e.clk.advance(3 * time.Second)
	e.c.Sweep()
	if got := strings.Count(e.log.String(), "RemovePeer k45"); got != 1 {
		t.Errorf("the peer was re-added again (%d)", got)
	}
	if !e.tun.hasPeer(k) {
		t.Error("the peer is missing after the re-add")
	}

	e2 := newEnv(t)
	mustReady(t, e2.add(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e2.clk.advance(1000 * time.Millisecond)
	e2.tun.setHandshake(k, e2.clk.wallTime())
	e2.clk.advance(700 * time.Millisecond)
	e2.c.Sweep()
	if strings.Contains(e2.log.String(), "RemovePeer") {
		t.Error("a peer that had already handshaken was re-added")
	}
}

// An extend carries the other machine's current endpoint; a direct path follows
// it, a relayed one ignores it.
func TestExtendFollowsTheEndpointOnADirectPathOnly(t *testing.T) {
	e := newEnv(t)
	k := keyN(70)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	mustReady(t, e.extend(linkA, 600, sp("198.51.100.4:51820")), signal.CircleOpExtend)
	if p := e.tun.peer(k); p.Endpoint != "198.51.100.4:51820" {
		t.Errorf("direct peer endpoint = %s", p.Endpoint)
	}
	// A private address in an extend is not followed.
	mustReady(t, e.extend(linkA, 600, sp("192.168.1.5:51820")), signal.CircleOpExtend)
	if p := e.tun.peer(k); p.Endpoint != "198.51.100.4:51820" {
		t.Errorf("followed a private endpoint: %s", p.Endpoint)
	}
	// The kept configuration follows too.
	if kept := e.c.Kept(); len(kept) != 1 || kept[0].Endpoint != "198.51.100.4:51820" {
		t.Errorf("kept = %+v", kept)
	}

	e2 := newEnv(t)
	k2 := keyN(71)
	mustReady(t, e2.add(addMsg(linkA, signal.CircleRoleRequester, k2, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e2.extend(linkA, 600, sp("198.51.100.4:51820")), signal.CircleOpExtend)
	if p := e2.tun.peer(k2); p.Endpoint != synthetic(k2) {
		t.Errorf("a relayed peer followed a raw endpoint: %s", p.Endpoint)
	}
}

// LAN candidates: used only if the message supplied them AND the host is on that
// subnet, and never invented.
func TestLANCandidateUsedOnlyWhenSuppliedAndOnTheSameSubnet(t *testing.T) {
	lan := "192.168.1.23:51820"
	pick := func(c []string) (string, bool) {
		for _, x := range c {
			if x == lan {
				return x, true
			}
		}
		return "", false
	}
	e := newEnv(t, withPickLAN(pick))
	k := keyN(72)
	m := withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)
	m.PeerLocalEndpoints = []string{lan, "8.8.8.8:51820", "100.64.0.7:51820", "[fd00::1]:51820"}
	mustReady(t, e.add(m), signal.CircleOpAdd)
	if p := e.tun.peer(k); p.Endpoint != lan {
		t.Errorf("endpoint = %s, want the LAN candidate on the same subnet", p.Endpoint)
	}

	// Supplied, but this host is not on that subnet: the public endpoint.
	e2 := newEnv(t, withPickLAN(func([]string) (string, bool) { return "", false }))
	mustReady(t, e2.add(m), signal.CircleOpAdd)
	if p := e2.tun.peer(k); p.Endpoint != wanEP {
		t.Errorf("endpoint = %s, want the public endpoint when no candidate is on this host's subnet", p.Endpoint)
	}

	// No picker at all: a LAN candidate is never used.
	e3 := newEnv(t)
	mustReady(t, e3.add(m), signal.CircleOpAdd)
	if p := e3.tun.peer(k); p.Endpoint != wanEP {
		t.Errorf("endpoint = %s without a LAN picker", p.Endpoint)
	}

	// Under the relay-only policy a LAN candidate is not used either.
	e4 := newEnv(t, withPickLAN(pick))
	m2 := addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600)
	m2.PeerLocalEndpoints = []string{lan}
	mustReady(t, e4.add(m2), signal.CircleOpAdd)
	if p := e4.tun.peer(k); p.Endpoint != synthetic(k) {
		t.Errorf("relay-only policy used a LAN candidate: %s", p.Endpoint)
	}
}

// An endpoint that is not a public unicast address is not used. The link goes to
// the relay instead of failing.
func TestUnusableWANEndpointsAreIgnored(t *testing.T) {
	for _, ep := range []string{
		"192.168.1.5:51820", "10.0.0.1:51820", "172.16.4.4:51820", "127.0.0.1:51820",
		"169.254.1.1:51820", "100.64.0.7:51820", "100.127.255.255:51820", "0.0.0.0:51820",
		"0.1.2.3:51820", "240.0.0.1:51820", "255.255.255.255:51820", "224.0.0.1:51820",
		"203.0.113.9:0", "203.0.113.9", "[fe80::1%eth0]:51820", "[fe80::1]:51820", "[fd00::1]:51820",
		"[::1]:51820", "[::]:51820", "example.com:51820", "203.0.113.9:51820\nallowed_ip=0.0.0.0/0",
		"[::ffff:192.168.1.5]:51820", "",
	} {
		t.Run(ep, func(t *testing.T) {
			e := newEnv(t)
			k := keyN(73)
			mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), ep, 3000)), signal.CircleOpAdd)
			if p := e.tun.peer(k); p.Endpoint != synthetic(k) {
				t.Errorf("endpoint %q was used: peer = %+v", ep, p)
			}
		})
	}
	for _, ep := range []string{"203.0.113.9:51820", "198.51.100.4:1", "[2001:db8::1]:51820", "8.8.8.8:65535"} {
		t.Run("ok "+ep, func(t *testing.T) {
			e := newEnv(t)
			k := keyN(74)
			mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), ep, 3000)), signal.CircleOpAdd)
			if p := e.tun.peer(k); p.Endpoint != ep {
				t.Errorf("a public endpoint %q was not used: %+v", ep, p)
			}
		})
	}
}

func TestDirectWindowIsCappedAtTenSeconds(t *testing.T) {
	e := newEnv(t)
	k := keyN(75)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 600000)), signal.CircleOpAdd)
	e.clk.advance(10100 * time.Millisecond)
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) {
		t.Errorf("still direct after the 10s cap: %+v", p)
	}
}

// The requester starts the handshake at once; a machine that only lends waits
// 400 ms and then turns its keepalive on, so the two initiations do not collide.
func TestLenderOnlyStartsItsHandshakeAfterTheRequesterHadItsChance(t *testing.T) {
	for _, tc := range []struct {
		name   string
		direct bool
		want   string
	}{{"relay", false, "ka=25"}, {"direct", true, "ka=5"}} {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			k := keyN(90)
			m := addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)
			if tc.direct {
				m = withDirect(m, wanEP, 3000)
			}
			mustReady(t, e.add(m), signal.CircleOpAdd)
			if got := e.tun.peer(k).PersistentKeepalive; got != 0 {
				t.Fatalf("keepalive at add = %d, want 0 (the lender waits)", got)
			}
			e.clk.advance(350 * time.Millisecond)
			e.c.Sweep()
			if got := e.tun.peer(k).PersistentKeepalive; got != 0 {
				t.Fatalf("keepalive at 350ms = %d, want 0", got)
			}
			e.clk.advance(100 * time.Millisecond)
			e.log.reset()
			e.c.Sweep()
			if !strings.Contains(e.log.String(), tc.want) || strings.Contains(e.log.String(), "RemovePeer") {
				t.Errorf("at 450ms the lender should turn the keepalive on in place (%s), got: %s", tc.want, e.log)
			}
			if kept := e.c.Kept(); len(kept) != 1 || kept[0].PersistentKeepalive == 0 {
				t.Errorf("kept after the kick = %+v", kept)
			}
		})
	}

	// A requester, and a peer that is a requester as well as a lender, start at once.
	e := newEnv(t)
	k := keyN(91)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	if got := e.tun.peer(k).PersistentKeepalive; got != 25 {
		t.Errorf("requester keepalive at add = %d, want 25", got)
	}
	k2 := keyN(92)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleRequester, k2, circIP2, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.add(addMsg(linkC, signal.CircleRoleLender, k2, circIP2, 8340, 600)), signal.CircleOpAdd)
	if got := e.tun.peer(k2).PersistentKeepalive; got != 25 {
		t.Errorf("keepalive of a peer that is both requester and lender = %d, want 25", got)
	}

	// With nothing to send to there is nothing to start.
	e2 := newEnv(t, withoutRelay())
	k3 := keyN(93)
	mustReady(t, e2.add(addMsg(linkA, signal.CircleRoleLender, k3, circIP1, 8347, 600)), signal.CircleOpAdd)
	e2.clk.advance(time.Second)
	e2.c.Sweep()
	if got := e2.tun.peer(k3).PersistentKeepalive; got != 0 {
		t.Errorf("a peer with no transport got keepalive %d", got)
	}
}

// If the requester's handshake got there first, the kick does not start another:
// it only switches the keepalive on.
func TestKickWithASessionAlreadyUpDoesNotRestartThePeer(t *testing.T) {
	e := newEnv(t)
	k := keyN(94)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.clk.advance(200 * time.Millisecond)
	e.tun.setHandshake(k, e.clk.wallTime())
	e.clk.advance(250 * time.Millisecond)
	e.log.reset()
	e.c.Sweep()
	if strings.Contains(e.log.String(), "RemovePeer") {
		t.Errorf("a peer with a session was removed: %s", e.log)
	}
	if got := e.tun.peer(k).PersistentKeepalive; got != 25 {
		t.Errorf("keepalive after the kick = %d", got)
	}
}

// Whenever a lend-only peer is created again, the requester's initiation goes
// first: on the move to the relay and on the poke the lender comes back with the
// keepalive off and turns it on 400 ms later.
func TestLenderOnlyKeepsQuietWhenRecreatedForTheRelayAndThePoke(t *testing.T) {
	// The move to the relay.
	e := newEnv(t)
	k := keyN(95)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(3100 * time.Millisecond)
	e.c.Sweep()
	if p := e.tun.peer(k); p.Endpoint != synthetic(k) || p.PersistentKeepalive != 0 {
		t.Fatalf("lender after the move to the relay = %+v, want the relay endpoint with the keepalive off", p)
	}
	e.clk.advance(450 * time.Millisecond)
	e.c.Sweep()
	if p := e.tun.peer(k); p.PersistentKeepalive != 25 {
		t.Errorf("lender 450ms after the move = %+v, want keepalive 25", p)
	}

	// The poke.
	e2 := newEnv(t)
	k2 := keyN(96)
	mustReady(t, e2.add(addMsg(linkA, signal.CircleRoleLender, k2, circIP1, 8347, 600)), signal.CircleOpAdd)
	e2.clk.advance(500 * time.Millisecond)
	e2.c.Sweep() // the kick: keepalive on
	e2.clk.advance(1100 * time.Millisecond)
	e2.log.reset()
	e2.c.Sweep() // 1.6 s, no handshake: poke
	if !strings.Contains(e2.log.String(), "RemovePeer k60") || !strings.Contains(e2.log.String(), "ka=0") {
		t.Fatalf("poke operations: %s, want the peer removed and added again with the keepalive off", e2.log)
	}
	e2.clk.advance(450 * time.Millisecond)
	e2.c.Sweep()
	if p := e2.tun.peer(k2); p.PersistentKeepalive != 25 {
		t.Errorf("lender 450ms after the poke = %+v, want keepalive 25", p)
	}

	// A requester is never held back.
	e3 := newEnv(t)
	k3 := keyN(97)
	mustReady(t, e3.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k3, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e3.clk.advance(3100 * time.Millisecond)
	e3.c.Sweep()
	if p := e3.tun.peer(k3); p.Endpoint != synthetic(k3) || p.PersistentKeepalive != 25 {
		t.Errorf("requester after the move to the relay = %+v, want keepalive 25 at once", p)
	}
}
