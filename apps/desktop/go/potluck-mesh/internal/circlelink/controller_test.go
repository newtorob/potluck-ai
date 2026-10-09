package circlelink

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// ---------------------------------------------------------------- add, order, ack

// The lender's add: entry first, then the WireGuard peer, its relay flag and the
// host route, and only then the ready ack. The inbound port is the lender's own.
// A lender-only peer starts with the keepalive off, so the requester speaks first.
func TestAddLenderInstallsInOrderAndAcksReady(t *testing.T) {
	e := newEnv(t)
	k := keyN(1)
	ack := e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600))
	mustReady(t, ack, signal.CircleOpAdd)

	want := []string{
		"SetEntry 100.64.1.2 in=8347 out=0 ttl=10m0s",
		"AddPeer k01 ep=" + synthetic(k) + " ka=0",
		"SetPeerRouting 01 true",
		"AddPeerRoute 100.64.1.2",
		"ack add ready -",
	}
	if got := e.log.list(); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("operations:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	p := e.tun.peer(k)
	if len(p.AllowedIPs) != 1 || p.AllowedIPs[0] != "100.64.1.2/32" {
		t.Errorf("allowed-ips = %v, want exactly the peer's /32", p.AllowedIPs)
	}
	v := e.c.Peers()
	if len(v) != 1 || v[0].MeshIP != circIP1 || v[0].Inbound == nil || v[0].Outbound != nil {
		t.Fatalf("view = %+v", v)
	}
	if v[0].Inbound.LinkID != linkA || v[0].Inbound.Port != 8347 || v[0].Inbound.TokenSHA256 != tokenA || v[0].Inbound.ExpiresInS != 600 {
		t.Errorf("inbound = %+v", *v[0].Inbound)
	}
}

// The requester's add: outbound only, to the lender's port, and no token.
func TestAddRequesterAllowsOutboundOnly(t *testing.T) {
	e := newEnv(t)
	ack := e.add(addMsg(linkA, signal.CircleRoleRequester, keyN(2), circIP1, 8347, 120))
	mustReady(t, ack, signal.CircleOpAdd)
	if g, ok := e.flt.entry(circIP1); !ok || g.InPort != 0 || g.OutPort != 8347 || g.TTL != 120*time.Second {
		t.Errorf("entry = %+v, %v; want outbound 8347 for 120s", g, ok)
	}
	v := e.c.Peers()
	if len(v) != 1 || v[0].Outbound == nil || v[0].Inbound != nil || v[0].Outbound.Port != 8347 {
		t.Fatalf("view = %+v", v)
	}
}

// Hosts that lend to each other: one WireGuard peer, one filter entry with both
// ports, one view with inbound and outbound. Ending one side keeps the other.
func TestBothRolesOnOnePeerMerge(t *testing.T) {
	e := newEnv(t)
	k := keyN(3)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.clk.advance(100 * time.Second)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleRequester, k, circIP1, 8340, 300)), signal.CircleOpAdd)

	if n := strings.Count(e.log.String(), "AddPeer "); n != 1 {
		t.Errorf("the second side re-added the device peer (%d AddPeer calls)", n)
	}
	if n := strings.Count(e.log.String(), "AddPeerRoute"); n != 1 {
		t.Errorf("the second side re-added the route (%d calls)", n)
	}
	g, _ := e.flt.entry(circIP1)
	if g.InPort != 8347 || g.OutPort != 8340 {
		t.Fatalf("merged entry = %+v, want in 8347 out 8340", g)
	}
	if g.TTL != 500*time.Second {
		t.Errorf("merged entry deadline = %s, want the later of the two (500s left on the lender side)", g.TTL)
	}
	v := e.c.Peers()
	if len(v) != 1 || v[0].Inbound == nil || v[0].Outbound == nil {
		t.Fatalf("want one view with both sides, got %+v", v)
	}
	if v[0].Inbound.LinkID != linkA || v[0].Outbound.LinkID != linkB {
		t.Errorf("sides = %+v / %+v", *v[0].Inbound, *v[0].Outbound)
	}

	// The requester side ends: the peer stays for the lender side, and the entry
	// stops allowing the outbound port.
	e.remove(linkB, signal.CircleRemoveEnded)
	g, ok := e.flt.entry(circIP1)
	if !ok || g.InPort != 8347 || g.OutPort != 0 {
		t.Errorf("after the outbound side ended entry = %+v, %v", g, ok)
	}
	if !e.tun.hasPeer(k) || !e.tun.hasRoute(circIP1) {
		t.Error("the peer or its route was removed while the lender side lives")
	}
	e.remove(linkA, signal.CircleRemoveEnded)
	e.nothingHeld(k, circIP1)
}

// Removal: route, then the WireGuard peer, then the relay flag, then the filter
// entry last, so the entry confines the peer for as long as it can send.
func TestRemoveOrderRouteThenPeerThenRelayThenEntry(t *testing.T) {
	e := newEnv(t)
	k := keyN(4)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.log.reset()
	e.remove(linkA, signal.CircleRemoveRevoked)
	want := []string{
		"RemovePeerRoute 100.64.1.2",
		"RemovePeer k04",
		"SetPeerRouting 04 false",
		"RemoveEntry 100.64.1.2",
	}
	if got := e.log.list(); strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("operations:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	e.nothingHeld(k, circIP1)
}

// A remove for a link that is not held, twice, changes nothing and is not acked.
func TestRemoveIsIdempotentAndUnackedForUnknownLinks(t *testing.T) {
	e := newEnv(t)
	e.remove(linkC, signal.CircleRemoveEnded)
	k := keyN(5)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.remove(linkC, signal.CircleRemoveEnded)
	e.remove("not-a-uuid", signal.CircleRemoveEnded)
	if !e.tun.hasPeer(k) {
		t.Fatal("a remove for another link dropped the peer")
	}
	e.remove(linkA, signal.CircleRemoveEnded)
	e.remove(linkA, signal.CircleRemoveEnded)
	e.nothingHeld(k, circIP1)
}

// ---------------------------------------------------------------- extend

func TestExtendMovesTheDeadlineFromReceiptForBothRoles(t *testing.T) {
	for _, role := range []string{signal.CircleRoleLender, signal.CircleRoleRequester} {
		t.Run(role, func(t *testing.T) {
			e := newEnv(t)
			k := keyN(6)
			mustReady(t, e.add(addMsg(linkA, role, k, circIP1, 8347, 300)), signal.CircleOpAdd)
			e.clk.advance(200 * time.Second)
			ack := e.extend(linkA, 600, nil)
			mustReady(t, ack, signal.CircleOpExtend)
			g, _ := e.flt.entry(circIP1)
			if g.TTL != 600*time.Second {
				t.Errorf("filter ttl after extend = %s, want 600s from receipt", g.TTL)
			}
			// It would have expired at 300s; it must not.
			e.clk.advance(150 * time.Second)
			e.c.Sweep()
			if !e.tun.hasPeer(k) {
				t.Fatal("the peer expired at its old deadline after an extend")
			}
			e.clk.advance(460 * time.Second)
			e.c.Sweep()
			e.nothingHeld(k, circIP1)
		})
	}
}

func TestExtendClampsToThirtyMinutes(t *testing.T) {
	e := newEnv(t)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(7), circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.extend(linkA, 99999, nil), signal.CircleOpExtend)
	if g, _ := e.flt.entry(circIP1); g.TTL != 30*time.Minute {
		t.Errorf("ttl = %s, want 30m", g.TTL)
	}
	if v := e.c.Peers(); v[0].Inbound.ExpiresInS != 1800 {
		t.Errorf("expires_in_s = %d, want 1800", v[0].Inbound.ExpiresInS)
	}
}

func TestExtendOfAnUnknownLinkIsRejectedUnknownGrant(t *testing.T) {
	e := newEnv(t)
	mustReject(t, e.extend(linkC, 600, nil), signal.CircleOpExtend, signal.CircleReasonUnknownGrant)
	if e.flt.count() != 0 {
		t.Error("an extend for an unknown link created a filter entry")
	}
}

// An extend that arrives after the deadline is too late: the grant is gone and
// the extend is refused, not honoured.
func TestExtendAfterTheDeadlineDoesNotResurrect(t *testing.T) {
	e := newEnv(t)
	k := keyN(8)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 10)), signal.CircleOpAdd)
	e.clk.advance(11 * time.Second)
	mustReject(t, e.extend(linkA, 600, nil), signal.CircleOpExtend, signal.CircleReasonUnknownGrant)
	e.nothingHeld(k, circIP1)
}

// A failed filter update during an extend leaves the old deadline.
func TestExtendFilterFailureKeepsTheOldDeadline(t *testing.T) {
	e := newEnv(t)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(9), circIP1, 8347, 100)), signal.CircleOpAdd)
	e.flt.fail = errBoom
	mustReject(t, e.extend(linkA, 1000, nil), signal.CircleOpExtend, signal.CircleReasonTunnelError)
	e.flt.fail = nil
	if v := e.c.Peers(); v[0].Inbound.ExpiresInS != 100 {
		t.Errorf("deadline moved to %ds despite the failed extend", v[0].Inbound.ExpiresInS)
	}
}

// ---------------------------------------------------------------- supersede, replay

func TestSupersedeReplacesTheGrantAndALateRemoveOfTheOldLinkIsANoOp(t *testing.T) {
	e := newEnv(t)
	k := keyN(10)
	first := addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)
	mustReady(t, e.add(first), signal.CircleOpAdd)
	second := addMsg(linkB, signal.CircleRoleLender, k, circIP1, 8350, 300)
	second.TokenSHA256 = sp(tokenB)
	mustReady(t, e.add(second), signal.CircleOpAdd)

	v := e.c.Peers()
	if len(v) != 1 || v[0].Inbound.LinkID != linkB || v[0].Inbound.TokenSHA256 != tokenB || v[0].Inbound.Port != 8350 {
		t.Fatalf("the new grant did not replace the old one: %+v", v)
	}
	if g, _ := e.flt.entry(circIP1); g.InPort != 8350 || g.TTL != 300*time.Second {
		t.Errorf("entry = %+v, want port 8350 for 300s", g)
	}
	if n := strings.Count(e.log.String(), "AddPeer "); n != 1 {
		t.Errorf("a supersede re-added the device peer (%d AddPeer calls); its session must survive", n)
	}

	// The coordinator now removes the OLD link id with reason superseded.
	e.remove(linkA, signal.CircleRemoveSuperseded)
	if !e.tun.hasPeer(k) || !e.tun.hasRoute(circIP1) {
		t.Fatal("the late remove of the superseded link tore the peer down")
	}
	if _, ok := e.flt.entry(circIP1); !ok {
		t.Fatal("the late remove of the superseded link removed the filter entry")
	}
	if v := e.c.Peers(); len(v) != 1 || v[0].Inbound.LinkID != linkB {
		t.Fatalf("view after the late remove = %+v", v)
	}
	// An extend of the dead id is unknown.
	mustReject(t, e.extend(linkA, 600, nil), signal.CircleOpExtend, signal.CircleReasonUnknownGrant)
	e.remove(linkB, signal.CircleRemoveEnded)
	e.nothingHeld(k, circIP1)
}

// After a signal reconnect the coordinator sends removes for links that ended in
// the last half hour, then adds for the live ones with the remaining lifetime.
// A link already held is acknowledged again and stays one entry; a removal that
// was missed while the channel was down now applies.
func TestReplayAfterReconnectRemovesFirstThenAddsWithRemainingTTL(t *testing.T) {
	e := newEnv(t)
	live, missed := keyN(111), keyN(112)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, live, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleRequester, missed, circIP2, 8347, 600)), signal.CircleOpAdd)
	e.clk.advance(200 * time.Second)

	e.log.reset()
	// The replay: link B ended while the channel was down; link A is live with 400s left.
	e.remove(linkB, signal.CircleRemoveEnded)
	e.remove(linkC, signal.CircleRemoveEnded) // ended earlier and already gone here: ignored
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, live, circIP1, 8347, 400)), signal.CircleOpAdd)

	if e.tun.hasPeer(missed) {
		t.Error("the missed removal was not applied")
	}
	if !e.tun.hasPeer(live) {
		t.Fatal("the live link was dropped")
	}
	if n := strings.Count(e.log.String(), "AddPeer "); n != 0 {
		t.Errorf("the replayed add re-added the device peer (%d AddPeer calls)", n)
	}
	if v := e.c.Peers(); len(v) != 1 || v[0].Inbound.ExpiresInS != 400 {
		t.Errorf("view = %+v, want one peer with 400s left", v)
	}
	if e.flt.count() != 1 {
		t.Errorf("filter entries = %d, want 1", e.flt.count())
	}
}

// ---------------------------------------------------------------- expiry

func TestExpiryByTimerRemovesPeerRouteAndEntry(t *testing.T) {
	e := newEnv(t)
	k := keyN(13)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 60)), signal.CircleOpAdd)
	e.clk.advance(59 * time.Second)
	e.c.Sweep()
	if !e.tun.hasPeer(k) {
		t.Fatal("removed a second early")
	}
	e.clk.advance(2 * time.Second)
	e.log.reset()
	e.c.Sweep()
	e.nothingHeld(k, circIP1)
	want := "RemovePeerRoute 100.64.1.2 | RemovePeer k0d | SetPeerRouting 0d false | RemoveEntry 100.64.1.2"
	if got := e.log.String(); got != want {
		t.Errorf("teardown order:\n%s\nwant:\n%s", got, want)
	}
}

// A machine that slept past the deadline: the monotonic clock did not move, the
// wall clock jumped. The controller lets go on waking, and until it has swept it
// already reports and carries nothing.
func TestExpiryAfterSleepLetsGoOnWake(t *testing.T) {
	e := newEnv(t)
	k := keyN(14)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.clk.sleep(time.Hour)

	if v := e.c.Peers(); len(v) != 0 {
		t.Errorf("an expired link is listed before the sweep: %+v", v)
	}
	if kept := e.c.Kept(); len(kept) != 0 {
		t.Errorf("an expired link would survive a welcome: %+v", kept)
	}
	if !e.tun.hasPeer(k) {
		t.Fatal("setup: the peer should still be on the device until the sweep")
	}
	e.c.Sweep()
	e.nothingHeld(k, circIP1)
}

// The wall clock moving backwards must not extend a link: the monotonic
// deadline still holds.
func TestExpiryWallClockMovedBackDoesNotExtend(t *testing.T) {
	e := newEnv(t)
	k := keyN(15)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 60)), signal.CircleOpAdd)
	e.clk.mu.Lock()
	e.clk.mono += int64(61 * time.Second)
	e.clk.wall -= int64(time.Hour)
	e.clk.mu.Unlock()
	e.c.Sweep()
	e.nothingHeld(k, circIP1)
}

// With both sides held, the side that ends first is retired on its own clock and
// the longer side keeps working.
func TestExpiryOfOneSideKeepsTheOther(t *testing.T) {
	e := newEnv(t)
	k := keyN(16)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 60)), signal.CircleOpAdd)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleRequester, k, circIP1, 8340, 600)), signal.CircleOpAdd)
	e.clk.advance(61 * time.Second)
	e.c.Sweep()
	if !e.tun.hasPeer(k) {
		t.Fatal("the peer went with the shorter side")
	}
	g, _ := e.flt.entry(circIP1)
	if g.InPort != 0 || g.OutPort != 8340 {
		t.Errorf("entry after the lender side expired = %+v, want outbound only", g)
	}
	v := e.c.Peers()
	if len(v) != 1 || v[0].Inbound != nil || v[0].Outbound == nil {
		t.Errorf("view = %+v", v)
	}
	e.clk.advance(600 * time.Second)
	e.c.Sweep()
	e.nothingHeld(k, circIP1)
}

// The Run loop sleeps until something is held, ticks while it is, and applies
// the sweep. The timer is injected.
func TestRunLoopSweepsOnTicks(t *testing.T) {
	e := newEnv(t)
	ticks := make(chan time.Time)
	waits := make(chan time.Duration, 8)
	e.c.cfg.After = func(d time.Duration) <-chan time.Time { waits <- d; return ticks }
	done := make(chan struct{})
	ctx, cancel := ctxWithCancel()
	go func() { e.c.Run(ctx); close(done) }()

	k := keyN(17)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 60)), signal.CircleOpAdd)
	select {
	case d := <-waits:
		if d != fastTick {
			t.Errorf("first wait = %s, want the fast tick while a poke is pending", d)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the run loop never started ticking")
	}
	e.clk.advance(61 * time.Second)
	ticks <- time.Now()
	waitFor(t, "the run loop swept the expired link", func() bool { return !e.tun.hasPeer(k) })
	cancel()
	<-done
}

// ---------------------------------------------------------------- what a welcome keeps

func TestKeptCarriesLivePeersInTheirCurrentConfigAndNothingElse(t *testing.T) {
	e := newEnv(t)
	k1, k2, k3 := keyN(18), keyN(19), keyN(20)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k1, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.add(withDirect(addMsg(linkB, signal.CircleRoleRequester, k2, circIP2, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	short := addMsg(linkC, signal.CircleRoleLender, k3, "100.64.3.2", 8347, 30)
	mustReady(t, e.add(short), signal.CircleOpAdd)

	kept := e.c.Kept()
	if len(kept) != 3 {
		t.Fatalf("kept %d peers, want 3: %+v", len(kept), kept)
	}
	byKey := map[string]tunnel.Peer{}
	for _, p := range kept {
		byKey[p.PublicKey] = p
	}
	if p := byKey[k1]; p.Endpoint != synthetic(k1) || p.PersistentKeepalive != 0 || p.AllowedIPs[0] != circIP1+"/32" || !p.Private {
		t.Errorf("relay peer kept as %+v (a lender-only peer waits to speak, so no keepalive yet)", p)
	}
	if p := byKey[k2]; p.Endpoint != wanEP || p.PersistentKeepalive != 5 {
		t.Errorf("direct peer kept as %+v", p)
	}
	// The short link expires; a welcome in that moment must not bring it back.
	e.clk.advance(31 * time.Second)
	for _, p := range e.c.Kept() {
		if p.PublicKey == k3 {
			t.Error("an expired link is still kept")
		}
	}
	// A removed link is not kept either.
	e.remove(linkA, signal.CircleRemoveEnded)
	for _, p := range e.c.Kept() {
		if p.PublicKey == k1 {
			t.Error("a removed link is still kept")
		}
	}
}

// A peer's transport changes (the relay takes over); what a welcome keeps follows.
func TestKeptFollowsTheTransport(t *testing.T) {
	e := newEnv(t)
	k := keyN(21)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleRequester, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(3100 * time.Millisecond)
	e.c.Sweep()
	kept := e.c.Kept()
	if len(kept) != 1 || kept[0].Endpoint != synthetic(k) || kept[0].PersistentKeepalive != 25 {
		t.Errorf("after the fallback the kept config is %+v", kept)
	}
}

// ---------------------------------------------------------------- household collisions, unpair

func TestHouseholdEntryWithTheSameAddressRemovesTheCirclePeer(t *testing.T) {
	e := newEnv(t)
	k1, k2 := keyN(22), keyN(23)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k1, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleLender, k2, circIP2, 8347, 600)), signal.CircleOpAdd)
	e.log.reset()

	e.c.HouseholdChanged("", []signal.RosterPeer{{NodeID: "n-x", MeshIP: circIP1, WGPublicKey: keyN(0x77)}})

	e.log.mu.Lock()
	got := strings.Join(e.log.ops, " | ")
	e.log.mu.Unlock()
	want := "RemovePeerRoute 100.64.1.2 | RemovePeer k16 | SetPeerRouting 16 false | RemoveEntry 100.64.1.2"
	if got != want {
		t.Errorf("operations:\n%s\nwant:\n%s", got, want)
	}
	if !e.tun.hasPeer(k2) {
		t.Error("an unrelated circle peer was removed")
	}
	if v := e.c.Peers(); len(v) != 1 || v[0].MeshIP != circIP2 {
		t.Errorf("view = %+v", v)
	}
}

func TestHouseholdEntryWithTheSameKeyRemovesTheCirclePeer(t *testing.T) {
	e := newEnv(t)
	k := keyN(24)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.c.HouseholdChanged("", []signal.RosterPeer{{NodeID: "n-x", MeshIP: "100.64.0.9", WGPublicKey: k}})
	e.nothingHeld(k, circIP1)
}

// A household entry that does not touch a circle peer leaves it alone.
func TestHouseholdEntryElsewhereLeavesCirclePeersAlone(t *testing.T) {
	e := newEnv(t)
	k := keyN(25)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.c.HouseholdChanged(selfIP, []signal.RosterPeer{{NodeID: "n-x", MeshIP: "100.64.0.9", WGPublicKey: keyN(0x78)}, {NodeID: "n-y", MeshIP: "nonsense", WGPublicKey: "x"}})
	if !e.tun.hasPeer(k) {
		t.Fatal("an unrelated household change removed the circle peer")
	}
}

// If this node's own address moves into the circle peer's /24 (re-enrolled into
// another household), the circle peer goes.
func TestNewSelfAddressInsideThePeersRangeRemovesIt(t *testing.T) {
	e := newEnv(t)
	k := keyN(26)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.c.HouseholdChanged("100.64.1.9", nil)
	e.nothingHeld(k, circIP1)
}

func TestClearRemovesEveryCirclePeer(t *testing.T) {
	e := newEnv(t)
	k1, k2 := keyN(27), keyN(28)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k1, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleRequester, k2, circIP2, 8347, 600)), signal.CircleOpAdd)
	e.c.Clear()
	e.nothingHeld(k1, circIP1)
	e.nothingHeld(k2, circIP2)
	// The household partner and the household path were never touched.
	for _, op := range e.log.list() {
		if strings.Contains(op, hhIP) {
			t.Errorf("clear touched a household address: %s", op)
		}
	}
}

// ---------------------------------------------------------------- rejections

func TestEveryRejectionReason(t *testing.T) {
	k := keyN(30)
	good := func() signal.CirclePeerAdd { return addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600) }
	mod := func(f func(*signal.CirclePeerAdd)) signal.CirclePeerAdd { m := good(); f(&m); return m }

	cases := []struct {
		name   string
		msg    signal.CirclePeerAdd
		reason string
		tweak  func(*env)
	}{
		{"bad key (short)", mod(func(m *signal.CirclePeerAdd) { m.PeerWGPublicKey = "AAAA" }), signal.CircleReasonInvalidPeer, nil},
		{"bad key (not base64)", mod(func(m *signal.CirclePeerAdd) { m.PeerWGPublicKey = strings.Repeat("!", 44) }), signal.CircleReasonInvalidPeer, nil},
		{"key with a newline", mod(func(m *signal.CirclePeerAdd) { m.PeerWGPublicKey = k[:43] + "\n" }), signal.CircleReasonInvalidPeer, nil},
		{"own key", mod(func(m *signal.CirclePeerAdd) { m.PeerWGPublicKey = selfKey }), signal.CircleReasonInvalidPeer, nil},
		{"outside the mesh range", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "10.9.9.9" }), signal.CircleReasonInvalidPeer, nil},
		{"loopback", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "127.0.0.1" }), signal.CircleReasonInvalidPeer, nil},
		{"link local", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "169.254.169.254" }), signal.CircleReasonInvalidPeer, nil},
		{"pool broadcast", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "100.127.255.255" }), signal.CircleReasonInvalidPeer, nil},
		{"with a prefix", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "100.64.1.2/24" }), signal.CircleReasonInvalidPeer, nil},
		{"with an injected directive", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "100.64.1.2\nallowed_ip=0.0.0.0/0" }), signal.CircleReasonInvalidPeer, nil},
		{"with spaces", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = " 100.64.1.2 " }), signal.CircleReasonInvalidPeer, nil},
		{"ipv6", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "fd00::3" }), signal.CircleReasonInvalidPeer, nil},
		{"ipv4 mapped ipv6", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "::ffff:100.64.1.2" }), signal.CircleReasonInvalidPeer, nil},
		{"empty address", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "" }), signal.CircleReasonInvalidPeer, nil},
		{"port zero", mod(func(m *signal.CirclePeerAdd) { m.Port = 0 }), signal.CircleReasonInvalidPeer, nil},
		{"port too big", mod(func(m *signal.CirclePeerAdd) { m.Port = 70000 }), signal.CircleReasonInvalidPeer, nil},
		{"ttl zero", mod(func(m *signal.CirclePeerAdd) { m.TTLSeconds = 0 }), signal.CircleReasonInvalidPeer, nil},
		{"ttl negative", mod(func(m *signal.CirclePeerAdd) { m.TTLSeconds = -5 }), signal.CircleReasonInvalidPeer, nil},
		{"lender without a token hash", mod(func(m *signal.CirclePeerAdd) { m.TokenSHA256 = nil }), signal.CircleReasonInvalidPeer, nil},
		{"token hash in capitals", mod(func(m *signal.CirclePeerAdd) { m.TokenSHA256 = sp(strings.ToUpper(tokenA)) }), signal.CircleReasonInvalidPeer, nil},
		{"token hash too short", mod(func(m *signal.CirclePeerAdd) { m.TokenSHA256 = sp("abc") }), signal.CircleReasonInvalidPeer, nil},
		{"requester with a token hash", mod(func(m *signal.CirclePeerAdd) { m.Role = signal.CircleRoleRequester }), signal.CircleReasonInvalidPeer, nil},
		{"negative direct window", mod(func(m *signal.CirclePeerAdd) { m.DirectWindowMS = -1 }), signal.CircleReasonInvalidPeer, nil},
		{"the node's own address", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = selfIP }), signal.CircleReasonAddressConflict, nil},
		{"a household machine's address", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = hhIP }), signal.CircleReasonAddressConflict, nil},
		{"an address in the node's own range", mod(func(m *signal.CirclePeerAdd) { m.PeerMeshIP = "100.64.0.99" }), signal.CircleReasonAddressConflict, nil},
		{"a household machine's key", mod(func(m *signal.CirclePeerAdd) { m.PeerWGPublicKey = keyN(0x0B) }), signal.CircleReasonAddressConflict, nil},
		{"an unknown role", mod(func(m *signal.CirclePeerAdd) { m.Role = "observer" }), signal.CircleReasonUnsupported, nil},
		{"no mesh address yet", good(), signal.CircleReasonNotEnrolled, func(e *env) { e.self = netipZero() }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := newEnv(t)
			if tc.tweak != nil {
				tc.tweak(e)
			}
			ack := e.add(tc.msg)
			mustReject(t, ack, signal.CircleOpAdd, tc.reason)
			if e.flt.count() != 0 || len(e.tun.peers) != 0 || len(e.tun.routes) != 0 || len(e.c.Peers()) != 0 || len(e.c.Kept()) != 0 {
				t.Errorf("a refused add left something behind: %s", e.log)
			}
			if strings.Contains(e.log.String(), "AddPeer") || strings.Contains(e.log.String(), "SetEntry") {
				t.Errorf("a refused add reached the tunnel or the filter: %s", e.log)
			}
		})
	}
}

func TestUnsupportedWithoutAPacketFilter(t *testing.T) {
	e := newEnv(t, withoutFilter())
	mustReject(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(31), circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonUnsupported)
	if len(e.tun.peers) != 0 {
		t.Error("a peer was added with no packet filter to confine it")
	}
}

func TestAddressHeldByAnotherCircleKeyIsAConflictAndTheFirstPeerIsUntouched(t *testing.T) {
	e := newEnv(t)
	k1, k2 := keyN(32), keyN(33)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k1, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.log.reset()
	mustReject(t, e.add(addMsg(linkB, signal.CircleRoleLender, k2, circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonAddressConflict)
	if got := e.log.String(); got != "ack add rejected address_conflict" {
		t.Errorf("a conflicting add touched the tunnel: %s", got)
	}
	if !e.tun.hasPeer(k1) || e.tun.hasPeer(k2) {
		t.Error("the conflict changed which peer is on the device")
	}
	if v := e.c.Peers(); len(v) != 1 || v[0].Inbound.LinkID != linkA {
		t.Errorf("view = %+v", v)
	}
}

func TestKeyHeldAtAnotherAddressIsAConflict(t *testing.T) {
	e := newEnv(t)
	k := keyN(34)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReject(t, e.add(addMsg(linkB, signal.CircleRoleLender, k, circIP2, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonAddressConflict)
	if v := e.c.Peers(); len(v) != 1 || v[0].MeshIP != circIP1 {
		t.Errorf("view = %+v", v)
	}
}

func TestLinkIDInUseForAnotherPeerIsRejected(t *testing.T) {
	e := newEnv(t)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(35), circIP1, 8347, 600)), signal.CircleOpAdd)
	mustReject(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(36), circIP2, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonInvalidPeer)
}

func TestTTLAboveThirtyMinutesIsClamped(t *testing.T) {
	e := newEnv(t)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(37), circIP1, 8347, 86400)), signal.CircleOpAdd)
	if g, _ := e.flt.entry(circIP1); g.TTL != 30*time.Minute {
		t.Errorf("filter ttl = %s, want 30m", g.TTL)
	}
}

func TestCapOfSixteenPeers(t *testing.T) {
	e := newEnv(t)
	for i := 0; i < MaxPeers; i++ {
		link := fmt.Sprintf("c0ffee00-1111-4222-8333-%012d", i)
		mustReady(t, e.add(addMsg(link, signal.CircleRoleLender, keyN(byte(40+i)), fmt.Sprintf("100.64.%d.2", 10+i), 8347, 600)), signal.CircleOpAdd)
	}
	e.log.reset()
	mustReject(t, e.add(addMsg(linkB, signal.CircleRoleLender, keyN(99), "100.64.99.2", 8347, 600)), signal.CircleOpAdd, signal.CircleReasonLimit)
	if got := e.log.String(); got != "ack add rejected limit" {
		t.Errorf("the 17th add touched the tunnel: %s", got)
	}
	if len(e.c.Peers()) != MaxPeers {
		t.Errorf("peers = %d", len(e.c.Peers()))
	}
	// A second side on a peer already held is not a 17th peer.
	mustReady(t, e.add(addMsg(linkC, signal.CircleRoleRequester, keyN(40), "100.64.10.2", 8340, 600)), signal.CircleOpAdd)
	// Removing one frees a slot.
	e.remove("c0ffee00-1111-4222-8333-000000000001", signal.CircleRemoveEnded)
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleLender, keyN(99), "100.64.99.2", 8347, 600)), signal.CircleOpAdd)
}

// ---------------------------------------------------------------- rollback

func TestRollbackWhenTheFilterFails(t *testing.T) {
	e := newEnv(t)
	e.flt.fail = errBoom
	mustReject(t, e.add(addMsg(linkA, signal.CircleRoleLender, keyN(50), circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonTunnelError)
	if strings.Contains(e.log.String(), "AddPeer") {
		t.Errorf("the device was touched after the filter refused: %s", e.log)
	}
	e.flt.fail = nil
	e.nothingHeld(keyN(50), circIP1)
}

func TestRollbackWhenAddPeerFails(t *testing.T) {
	e := newEnv(t)
	k := keyN(51)
	e.tun.failOn("AddPeer", errBoom)
	mustReject(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonTunnelError)
	if strings.Contains(e.log.String(), "AddPeerRoute") {
		t.Errorf("the route was installed after the peer failed: %s", e.log)
	}
	e.nothingHeld(k, circIP1)
	// Nothing is stuck: the same add works once the tunnel does.
	e.tun.clearFail("AddPeer")
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
}

func TestRollbackWhenTheRouteFails(t *testing.T) {
	e := newEnv(t)
	k := keyN(52)
	e.tun.failOn("AddPeerRoute", errBoom)
	mustReject(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonTunnelError)
	e.nothingHeld(k, circIP1)
	e.tun.clearFail("AddPeerRoute")
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
}

// A failing filter update on a peer already held keeps the old grant.
func TestMergeFilterFailureKeepsTheExistingGrant(t *testing.T) {
	e := newEnv(t)
	k := keyN(53)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.flt.fail = errBoom
	mustReject(t, e.add(addMsg(linkB, signal.CircleRoleRequester, k, circIP1, 8340, 600)), signal.CircleOpAdd, signal.CircleReasonTunnelError)
	e.flt.fail = nil
	v := e.c.Peers()
	if len(v) != 1 || v[0].Outbound != nil || v[0].Inbound == nil {
		t.Fatalf("the failed merge changed the view: %+v", v)
	}
	// And the failed link id is not remembered as owning a side.
	e.remove(linkB, signal.CircleRemoveEnded)
	if !e.tun.hasPeer(k) {
		t.Error("a remove for the failed link dropped the held peer")
	}
}

// If the device refuses a removal the peer is retried, the filter entry is gone
// (so the address is denied), and the same key cannot be added again until the
// device has let go.
func TestFailedDeviceRemovalIsRetriedAndBlocksReuse(t *testing.T) {
	e := newEnv(t)
	k := keyN(54)
	mustReady(t, e.add(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
	e.tun.failOn("RemovePeer", errBoom)
	e.remove(linkA, signal.CircleRemoveRevoked)
	if _, ok := e.flt.entry(circIP1); ok {
		t.Error("the filter entry outlived the removal")
	}
	if len(e.c.Peers()) != 0 || len(e.c.Kept()) != 0 {
		t.Error("the peer is still listed or kept")
	}
	mustReject(t, e.add(addMsg(linkB, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd, signal.CircleReasonTunnelError)
	e.tun.clearFail("RemovePeer")
	e.c.Sweep()
	if e.tun.hasPeer(k) {
		t.Error("the retry did not remove the peer")
	}
	mustReady(t, e.add(addMsg(linkB, signal.CircleRoleLender, k, circIP1, 8347, 600)), signal.CircleOpAdd)
}

// ---------------------------------------------------------------- helpers

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", what)
}
