package main

import (
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/circlelink"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// These tests put a real circle controller next to the real roster handler and
// the recording tunnel of the roster tests, to show what a circle link does to
// the household path (nothing) and what a household roster change does to a
// circle link (it loses, before the tunnel is configured for the household).

// circleTun is the recording tunnel plus the one call the controller makes that
// the roster handler does not.
type circleTun struct{ *fakeTunnel }

func (circleTun) PeerEndpoint(string) (string, error) { return "", nil }

// circleFilter keeps the entry table and notes every change in the tunnel's op
// log, so the order across the two is visible.
type circleFilter struct {
	tun     *fakeTunnel
	mu      sync.Mutex
	entries map[netip.Addr]tunnel.Grant
}

func (f *circleFilter) SetEntry(a netip.Addr, g tunnel.Grant) error {
	f.tun.mu.Lock()
	f.tun.rec(fmt.Sprintf("filter.SetEntry:%s", a))
	f.tun.mu.Unlock()
	f.mu.Lock()
	f.entries[a] = g
	f.mu.Unlock()
	return nil
}
func (f *circleFilter) RemoveEntry(a netip.Addr) {
	f.tun.mu.Lock()
	f.tun.rec(fmt.Sprintf("filter.RemoveEntry:%s", a))
	f.tun.mu.Unlock()
	f.mu.Lock()
	delete(f.entries, a)
	f.mu.Unlock()
}
func (f *circleFilter) has(a string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.entries[netip.MustParseAddr(a)]
	return ok
}

type ackRecorder struct {
	mu   sync.Mutex
	acks []msignal.CirclePeerAck
}

func (a *ackRecorder) SendCirclePeerAck(x msignal.CirclePeerAck) {
	a.mu.Lock()
	a.acks = append(a.acks, x)
	a.mu.Unlock()
}
func (a *ackRecorder) last() msignal.CirclePeerAck {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.acks[len(a.acks)-1]
}

type circleFixture struct {
	*rosterFixture
	tt   *testing.T
	c    *circlelink.Controller
	flt  *circleFilter
	acks *ackRecorder
}

const (
	circLinkA = "c0ffee00-1111-4222-8333-444455556666"
	circLinkB = "d00dfeed-1111-4222-8333-444455556666"
	circToken = "45827150f595bab19278b82d22eba203d126486c59732655abb65ea67d209ee6"
)

// newCircleFixture welcomes a household with one machine (linux, 100.64.0.7) and
// builds a circle controller beside it, wired to the handler.
func newCircleFixture(t *testing.T) *circleFixture {
	t.Helper()
	fx := newRosterFixture(t)
	fx.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1)))
	fx.drain()
	flt := &circleFilter{tun: fx.tun, entries: map[netip.Addr]tunnel.Grant{}}
	acks := &ackRecorder{}
	c := circlelink.New(circlelink.Config{
		Tunnel:    circleTun{fx.tun},
		Filter:    flt,
		Signal:    acks,
		Household: fx.h.ps,
		Self:      func() (netip.Addr, string) { return netip.MustParseAddr(selfIP), testKey(0xEE) },
	})
	fx.h.setCircle(c)
	return &circleFixture{rosterFixture: fx, tt: t, c: c, flt: flt, acks: acks}
}

func (f *circleFixture) addLender(link, key string, seed byte, ip string) {
	f.c.OnCirclePeerAdd(msignal.CirclePeerAdd{Type: "circle_peer_add", LinkID: link, Role: "lender", PeerWGPublicKey: testKey(seed),
		PeerMeshIP: ip, Port: 8347, TTLSeconds: 600, TokenSHA256: &[]string{circToken}[0], PeerLocalEndpoints: []string{}})
	if a := f.acks.last(); a.State != "ready" {
		f.tt.Fatalf("add refused: %+v", a)
	}
}

func indexOf(ops []string, prefix string, from int) int {
	for i := from; i < len(ops); i++ {
		if strings.HasPrefix(ops[i], prefix) {
			return i
		}
	}
	return -1
}

func (f *circleFixture) opsSnapshot() []string {
	f.tun.mu.Lock()
	defer f.tun.mu.Unlock()
	return append([]string(nil), f.tun.ops...)
}

// A whole circle link, its extension and its removal, change nothing the
// household sees: no event, no store entry, no SetPeers, no DERP flip, no
// reachability or punch bookkeeping.
func TestCircleLifecyclePublishesNothingOnTheHouseholdPath(t *testing.T) {
	fx := newCircleFixture(t)
	storeBefore := storeIDs(fx.h)
	flipsBefore := len(*fx.flipped)
	setPeersBefore := len(fx.tun.setPeers)
	punchBefore := len(fx.sig.requests)

	lend := func(link string, seed byte, ip string) {
		fx.c.OnCirclePeerAdd(msignal.CirclePeerAdd{Type: "circle_peer_add", LinkID: link, Role: "lender", PeerWGPublicKey: testKey(seed),
			PeerMeshIP: ip, Port: 8347, TTLSeconds: 600, TokenSHA256: &[]string{circToken}[0], PeerLocalEndpoints: []string{}})
		if a := fx.acks.last(); a.State != "ready" {
			t.Fatalf("add refused: %+v", a)
		}
	}
	lend(circLinkA, 0x21, "100.64.1.2")
	fx.c.OnCirclePeerAdd(msignal.CirclePeerAdd{Type: "circle_peer_add", LinkID: circLinkB, Role: "requester", PeerWGPublicKey: testKey(0x22),
		PeerMeshIP: "100.64.2.2", Port: 8347, TTLSeconds: 600, PeerLocalEndpoints: []string{}})
	fx.c.OnCirclePeerExtend(msignal.CirclePeerExtend{Type: "circle_peer_extend", LinkID: circLinkA, TTLSeconds: 900})
	fx.c.OnCirclePeerRemove(msignal.CirclePeerRemove{Type: "circle_peer_remove", LinkID: circLinkA, Reason: "ended"})
	fx.c.OnCirclePeerRemove(msignal.CirclePeerRemove{Type: "circle_peer_remove", LinkID: circLinkB, Reason: "revoked"})
	lend(circLinkA, 0x23, "100.64.3.2")
	fx.c.Clear()

	if evs := fx.drain(); len(evs) != 0 {
		t.Errorf("the circle link published %d events on the bus: %+v", len(evs), evs)
	}
	wantStrings(t, "household store", storeIDs(fx.h), storeBefore)
	if n := len(*fx.flipped); n != flipsBefore {
		t.Errorf("the household DERP flip callback ran %d times for circle peers", n-flipsBefore)
	}
	if n := len(fx.tun.setPeers); n != setPeersBefore {
		t.Errorf("SetPeers ran %d extra times", n-setPeersBefore)
	}
	if n := len(fx.sig.requests); n != punchBefore {
		t.Errorf("the circle link caused %d request_punch messages", n-punchBefore)
	}
	// Nothing in the ops log touched the household machine.
	for _, op := range fx.opsSnapshot() {
		if strings.Contains(op, "100.64.0.7") && strings.Contains(op, "Remove") {
			t.Errorf("a circle operation touched the household peer: %s", op)
		}
	}
}

// A welcome that gives a household machine the address a circle peer holds
// removes the circle peer first: its route and device peer are gone before
// SetPeers and before the household route is installed.
func TestWelcomeThatClaimsACirclePeersAddressRemovesTheCirclePeerFirst(t *testing.T) {
	fx := newCircleFixture(t)
	circleKey := testKey(0x31)
	fx.c.OnCirclePeerAdd(msignal.CirclePeerAdd{Type: "circle_peer_add", LinkID: circLinkA, Role: "lender", PeerWGPublicKey: circleKey,
		PeerMeshIP: "100.64.1.2", Port: 8347, TTLSeconds: 600, TokenSHA256: &[]string{circToken}[0], PeerLocalEndpoints: []string{}})
	if !fx.flt.has("100.64.1.2") {
		t.Fatal("setup: no filter entry")
	}
	mark := len(fx.opsSnapshot())

	fx.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("newcomer", "100.64.1.2", 0x32)))

	ops := fx.opsSnapshot()[mark:]
	rmRoute := indexOf(ops, "RemovePeerRoute:100.64.1.2", 0)
	rmPeer := indexOf(ops, "RemovePeer:"+circleKey, 0)
	setPeers := indexOf(ops, "SetPeers:", 0)
	addRoute := indexOf(ops, "AddPeerRoute:100.64.1.2", 0)
	rmEntry := indexOf(ops, "filter.RemoveEntry:100.64.1.2", 0)
	for name, i := range map[string]int{"RemovePeerRoute": rmRoute, "RemovePeer": rmPeer, "SetPeers": setPeers, "AddPeerRoute": addRoute, "RemoveEntry": rmEntry} {
		if i < 0 {
			t.Fatalf("%s never ran: %v", name, ops)
		}
	}
	if !(rmRoute < rmPeer && rmPeer < rmEntry && rmEntry < setPeers && setPeers < addRoute) {
		t.Errorf("order wrong (route %d, peer %d, entry %d, SetPeers %d, household route %d): %v", rmRoute, rmPeer, rmEntry, setPeers, addRoute, ops)
	}
	if fx.flt.has("100.64.1.2") {
		t.Error("the circle peer's filter entry outlived the collision")
	}
	if v := fx.c.Peers(); len(v) != 0 {
		t.Errorf("circle view = %+v", v)
	}
	wantStrings(t, "household store", storeIDs(fx.h), []string{"linux", "newcomer"})
}

func TestRosterUpdateThatClaimsACirclePeersAddressRemovesTheCirclePeerBeforeAddPeer(t *testing.T) {
	fx := newCircleFixture(t)
	circleKey := testKey(0x33)
	fx.addLender(circLinkA, circleKey, 0x33, "100.64.1.2")
	mark := len(fx.opsSnapshot())

	fx.h.OnRosterUpdate(msignal.RosterUpdate{Type: "roster_update", Added: []msignal.RosterPeer{rp("newcomer", "100.64.1.2", 0x34)}})

	ops := fx.opsSnapshot()[mark:]
	rmPeer := indexOf(ops, "RemovePeer:"+circleKey, 0)
	addPeer := indexOf(ops, "AddPeer:"+testKey(0x34), 0)
	rmRoute := indexOf(ops, "RemovePeerRoute:100.64.1.2", 0)
	addRoute := indexOf(ops, "AddPeerRoute:100.64.1.2", 0)
	if rmPeer < 0 || addPeer < 0 || rmRoute < 0 || addRoute < 0 || !(rmRoute < rmPeer && rmPeer < addPeer && addPeer < addRoute) {
		t.Errorf("order wrong (remove route %d, remove peer %d, add peer %d, add route %d): %v", rmRoute, rmPeer, addPeer, addRoute, ops)
	}
	if fx.flt.has("100.64.1.2") || len(fx.c.Peers()) != 0 {
		t.Error("the circle peer survived the household claim")
	}
}

// A roster entry the store refuses is not a household machine, so it takes
// nothing from a circle peer even if it names the circle peer's key.
func TestRefusedRosterEntryLeavesCirclePeersAlone(t *testing.T) {
	fx := newCircleFixture(t)
	fx.addLender(circLinkA, testKey(0x35), 0x35, "100.64.1.2")

	twin := rp("self-twin", selfIP, 0x35) // claims this node's own address and the circle peer's key
	fx.h.OnRosterUpdate(msignal.RosterUpdate{Type: "roster_update", Added: []msignal.RosterPeer{twin}})

	if len(fx.c.Peers()) != 1 || !fx.flt.has("100.64.1.2") {
		t.Error("a refused roster entry removed the circle peer")
	}
}

// A welcome that does not touch the circle peer's address or key leaves it.
func TestWelcomeLeavesUnrelatedCirclePeersAlone(t *testing.T) {
	fx := newCircleFixture(t)
	circleKey := testKey(0x36)
	fx.addLender(circLinkA, circleKey, 0x36, "100.64.1.2")
	mark := len(fx.opsSnapshot())

	fx.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))

	for _, op := range fx.opsSnapshot()[mark:] {
		if strings.Contains(op, circleKey) || strings.Contains(op, "100.64.1.2") {
			t.Errorf("a welcome touched the circle peer: %s", op)
		}
	}
	if len(fx.c.Peers()) != 1 {
		t.Error("the circle peer is gone")
	}
}

type noopRoster struct{}

func (noopRoster) OnWelcome(msignal.Welcome)           {}
func (noopRoster) OnRosterUpdate(msignal.RosterUpdate) {}
func (noopRoster) OnPunchCoord(msignal.PunchCoord)     {}

// keepRecorder is a tunnel that remembers the kept-peers source it was handed.
type keepRecorder struct {
	circleTun
	keep func() []tunnel.Peer
}

func (k *keepRecorder) SetKeptPeers(fn func() []tunnel.Peer) { k.keep = fn }

// The shipped wiring builds a controller from a tunnel, a filter, a store, a
// client and the local API state, with no relay. It hands the tunnel the
// controller's kept peers, which is what stops a welcome (SetPeers) from dropping
// a live circle peer, and what a peer the controller holds looks like through it.
func TestSetupCircleLinksHandsTheTunnelTheControllersKeptPeers(t *testing.T) {
	ps := peerstate.New()
	ps.Reset(selfIP, nil)
	filter := tunnel.NewPacketFilter(ps.IsHousehold)
	filter.SetSelf(netip.MustParseAddr(selfIP).AsSlice())
	client := msignal.NewClient("ws://127.0.0.1:1/mesh/signal", "pk", "", nil, noopRoster{})
	ipc := localapi.NewState()
	ipc.SetWelcome("node", "hh", selfIP, testKey(0xEE))
	tun := &keepRecorder{circleTun: circleTun{&fakeTunnel{}}}
	c := setupCircleLinks(circleDeps{tun: tun, filter: filter, ps: ps, client: client, ipc: ipc})
	if c == nil {
		t.Fatal("no controller")
	}
	if tun.keep == nil {
		t.Fatal("the tunnel was not given a kept-peers source, so a welcome would drop every circle peer")
	}
	if got := tun.keep(); len(got) != 0 {
		t.Fatalf("kept peers before any link: %+v", got)
	}
	// A good add is held, and kept.
	c.OnCirclePeerAdd(msignal.CirclePeerAdd{Type: "circle_peer_add", LinkID: circLinkA, Role: "lender", PeerWGPublicKey: testKey(0x41),
		PeerMeshIP: "100.64.1.2", Port: 8347, TTLSeconds: 600, TokenSHA256: &[]string{circToken}[0]})
	got := tun.keep()
	if len(got) != 1 || got[0].PublicKey != testKey(0x41) || got[0].AllowedIPs[0] != "100.64.1.2/32" || !got[0].Private {
		t.Fatalf("kept peers after an add = %+v", got)
	}
	if len(filter.Entries()) != 1 {
		t.Errorf("the real filter holds %d entries", len(filter.Entries()))
	}
	c.Clear()
	if got := tun.keep(); len(got) != 0 {
		t.Errorf("kept peers after Clear = %+v", got)
	}
}
