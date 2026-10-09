package main

import (
	"fmt"
	"net/netip"
	"strings"
	"sync"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// The TUN packet filter lets a packet through when its peer address is in the
// peer store's household set. These tests drive OnWelcome and OnRosterUpdate
// against a tunnel that, at every call that adds, removes or routes a
// WireGuard peer, reads the set the filter would read at that instant. They pin
// the ordering the filter depends on:
//
//   - a peer is in the set before the tunnel is asked to add it, route it or
//     (on a same-key rejoin) remove and re-add it, so household traffic is never
//     dropped for lack of an entry;
//   - a peer leaves the set before the tunnel is asked to remove it, so the
//     filter stops passing it no later than the WireGuard peer goes away.

// memberTunnel wraps the recording fakeTunnel and notes, per call, whether the
// peer's address was in the set at that moment.
type memberTunnel struct {
	*fakeTunnel
	ps   *peerstate.Store
	ipOf map[string]string // wg key -> mesh ip, for calls that carry only a key

	mu    sync.Mutex
	calls []string
}

func (m *memberTunnel) member(ip string) bool {
	a, err := netip.ParseAddr(ip)
	return err == nil && m.ps.IsHousehold(a.As4())
}
func (m *memberTunnel) note(op, ip string) {
	m.mu.Lock()
	m.calls = append(m.calls, fmt.Sprintf("%s %s member=%v", op, ip, m.member(ip)))
	m.mu.Unlock()
}
func (m *memberTunnel) SetPeers(p []tunnel.Peer) error {
	for _, x := range p {
		m.note("SetPeers", strings.TrimSuffix(x.AllowedIPs[0], "/32"))
	}
	return m.fakeTunnel.SetPeers(p)
}
func (m *memberTunnel) AddPeer(p tunnel.Peer) error {
	m.note("AddPeer", strings.TrimSuffix(p.AllowedIPs[0], "/32"))
	return m.fakeTunnel.AddPeer(p)
}
func (m *memberTunnel) RemovePeer(k string) error {
	m.note("RemovePeer", m.ipOf[k])
	return m.fakeTunnel.RemovePeer(k)
}
func (m *memberTunnel) AddPeerRoute(ip string) error {
	m.note("AddPeerRoute", ip)
	return m.fakeTunnel.AddPeerRoute(ip)
}
func (m *memberTunnel) RemovePeerRoute(ip string) error {
	m.note("RemovePeerRoute", ip)
	return m.fakeTunnel.RemovePeerRoute(ip)
}
func (m *memberTunnel) take() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := m.calls
	m.calls = nil
	return out
}

func newMemberFixture() (*rosterHandler, *memberTunnel) {
	ps := peerstate.New()
	mt := &memberTunnel{fakeTunnel: &fakeTunnel{}, ps: ps, ipOf: map[string]string{}}
	h := newRosterHandler(mt, ps, nil, nil)
	h.client = &recoverySignal{}
	return h, mt
}

func (m *memberTunnel) know(p msignal.RosterPeer) { m.ipOf[p.WGPublicKey] = p.MeshIP }

func wantCalls(t *testing.T, what string, got []string, want ...string) {
	t.Helper()
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Errorf("%s:\n got:\n  %s\nwant:\n  %s", what, strings.Join(got, "\n  "), strings.Join(want, "\n  "))
	}
}

func TestHouseholdSet_WelcomeAddsBeforeTheTunnelSeesThePeers(t *testing.T) {
	h, mt := newMemberFixture()
	a, b := rp("a", "100.64.0.7", 1), rp("b", "100.64.0.8", 2)
	mt.know(a)
	mt.know(b)
	h.OnWelcome(welcome(a, b))
	wantCalls(t, "welcome", mt.take(),
		"SetPeers 100.64.0.7 member=true",
		"SetPeers 100.64.0.8 member=true",
		"AddPeerRoute 100.64.0.7 member=true",
		"AddPeerRoute 100.64.0.8 member=true",
	)
}

// A welcome after a reconnect carries the peers again: the ones that stay are
// in the set at every call, the one that left is out of it before the tunnel
// is rewritten.
func TestHouseholdSet_SecondWelcomeKeepsSurvivorsAndDropsTheRest(t *testing.T) {
	h, mt := newMemberFixture()
	a, b := rp("a", "100.64.0.7", 1), rp("b", "100.64.0.8", 2)
	mt.know(a)
	mt.know(b)
	h.OnWelcome(welcome(a, b))
	mt.take()
	h.OnWelcome(welcome(a)) // b is gone
	wantCalls(t, "second welcome", mt.take(),
		"SetPeers 100.64.0.7 member=true",
		"AddPeerRoute 100.64.0.7 member=true",
	)
	if mt.member("100.64.0.8") {
		t.Error("a peer that left the roster is still in the household set")
	}
}

// A welcome with an entry the store refuses: the refused address is never in the set.
func TestHouseholdSet_RefusedEntryNeverEntersTheSet(t *testing.T) {
	h, mt := newMemberFixture()
	good, bad := rp("good", "100.64.0.7", 1), rp("bad", "192.168.1.1", 2)
	mt.know(good)
	h.OnWelcome(welcome(good, bad))
	if mt.member("192.168.1.1") || !mt.member("100.64.0.7") {
		t.Errorf("set after the welcome: bad=%v good=%v", mt.member("192.168.1.1"), mt.member("100.64.0.7"))
	}
}

func TestHouseholdSet_RosterUpdateOrdering(t *testing.T) {
	h, mt := newMemberFixture()
	a := rp("a", "100.64.0.7", 1)
	mt.know(a)
	h.OnWelcome(welcome(a))
	mt.take()

	// A new peer: in the set before AddPeer and AddPeerRoute.
	b := rp("b", "100.64.0.8", 2)
	mt.know(b)
	h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{b}})
	wantCalls(t, "added", mt.take(),
		"AddPeer 100.64.0.8 member=true",
		"AddPeerRoute 100.64.0.8 member=true",
	)

	// A same-key rejoin: the peer stays in the set through RemovePeer and the re-add.
	h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{b}})
	wantCalls(t, "same-key rejoin", mt.take(),
		"RemovePeer 100.64.0.8 member=true",
		"AddPeer 100.64.0.8 member=true",
		"AddPeerRoute 100.64.0.8 member=true",
	)

	// An endpoint update of a held peer touches no membership.
	b2 := b
	ep := "203.0.113.99:51820"
	b2.LastEndpoint = &ep
	h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{b2}})
	if !mt.member("100.64.0.8") {
		t.Error("an endpoint update removed a peer from the set")
	}
	mt.take()

	// A removal: gone from the set before RemovePeer.
	h.OnRosterUpdate(msignal.RosterUpdate{Removed: []string{"b"}})
	wantCalls(t, "removed", mt.take(),
		"RemovePeer 100.64.0.8 member=false",
		"RemovePeerRoute 100.64.0.8 member=false",
	)
	if !mt.member("100.64.0.7") {
		t.Error("removing one peer took another out of the set")
	}
}

// An Updated entry for a node the store did not hold is set up as a new peer:
// it is in the set before AddPeer.
func TestHouseholdSet_UpdatedEntryOfAnUnheldNodeIsAddedAfterItsMembership(t *testing.T) {
	h, mt := newMemberFixture()
	a := rp("a", "100.64.0.7", 1)
	mt.know(a)
	h.OnWelcome(welcome(a))
	mt.take()
	c := rp("c", "100.64.0.9", 3)
	mt.know(c)
	h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{c}})
	wantCalls(t, "updated for an unheld node", mt.take(),
		"AddPeer 100.64.0.9 member=true",
		"AddPeerRoute 100.64.0.9 member=true",
	)
}

// Two nodes claim one address: neither keeps it. The peer the store held is out
// of the set before the tunnel removes it.
func TestHouseholdSet_ContestedAddressLeavesTheSetBeforeTheTunnelRemovesIt(t *testing.T) {
	h, mt := newMemberFixture()
	a := rp("a", "100.64.0.7", 1)
	mt.know(a)
	h.OnWelcome(welcome(a))
	mt.take()
	thief := rp("thief", "100.64.0.7", 9)
	h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{thief}})
	wantCalls(t, "contested address", mt.take(),
		"RemovePeer 100.64.0.7 member=false",
		"RemovePeerRoute 100.64.0.7 member=false",
	)
	if mt.member("100.64.0.7") {
		t.Error("a contested address is still in the household set")
	}
}

// The roster refresh loop reaches the same code through OnRosterUpdate; this
// pins that its additions and removals follow the same ordering.
func TestHouseholdSet_RefreshAddsAndRemovesThroughTheSameOrdering(t *testing.T) {
	h, mt := newMemberFixture()
	a := rp("a", "100.64.0.7", 1)
	mt.know(a)
	h.OnWelcome(welcome(a))
	mt.take()
	b := rp("b", "100.64.0.8", 2)
	mt.know(b)
	h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{b}, Removed: []string{"a"}})
	got := mt.take()
	want := []string{
		"AddPeer 100.64.0.8 member=true",
		"AddPeerRoute 100.64.0.8 member=true",
		"RemovePeer 100.64.0.7 member=false",
		"RemovePeerRoute 100.64.0.7 member=false",
	}
	wantCalls(t, "added and removed in one update", got, want...)
}
