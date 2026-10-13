package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// These tests drive the roster handler (OnWelcome / OnRosterUpdate /
// reconcileRosterOnce) against a recording rosterTunnel and check what reaches
// the tunnel, the host routes, the event bus, the DERP flips and the peer store
// when a roster contains entries that must be refused.

const selfIP = "100.64.0.6"

func testKey(seed byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = seed
	}
	return base64.StdEncoding.EncodeToString(b)
}

func rp(id, ip string, seed byte) msignal.RosterPeer {
	ep := fmt.Sprintf("203.0.113.%d:51820", seed)
	return msignal.RosterPeer{NodeID: id, NodeName: id, MeshIP: ip, WGPublicKey: testKey(seed), LastEndpoint: &ep}
}

// fakeTunnel records every rosterTunnel call. ops keeps them in call order.
type fakeTunnel struct {
	mu            sync.Mutex
	ops           []string
	meshIP        string
	setPeers      [][]tunnel.Peer
	added         []tunnel.Peer
	removedKeys   []string
	routesAdded   []string
	routesRemoved []string
	endpointCalls []string
}

func (f *fakeTunnel) rec(op string) { f.ops = append(f.ops, op) }

func (f *fakeTunnel) SetMeshIP(ip string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.meshIP = ip
	f.rec("SetMeshIP:" + ip)
	return nil
}
func (f *fakeTunnel) SetPeers(p []tunnel.Peer) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.setPeers = append(f.setPeers, p)
	f.rec(fmt.Sprintf("SetPeers:%d", len(p)))
	return nil
}
func (f *fakeTunnel) AddPeer(p tunnel.Peer) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.added = append(f.added, p)
	f.rec("AddPeer:" + p.PublicKey + ":" + strings.Join(p.AllowedIPs, ","))
	return nil
}
func (f *fakeTunnel) RemovePeer(k string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removedKeys = append(f.removedKeys, k)
	f.rec("RemovePeer:" + k)
	return nil
}
func (f *fakeTunnel) AddPeerRoute(ip string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.routesAdded = append(f.routesAdded, ip)
	f.rec("AddPeerRoute:" + ip)
	return nil
}
func (f *fakeTunnel) RemovePeerRoute(ip string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.routesRemoved = append(f.routesRemoved, ip)
	f.rec("RemovePeerRoute:" + ip)
	return nil
}
func (f *fakeTunnel) UpdatePeerEndpoint(k, ep string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.endpointCalls = append(f.endpointCalls, k+"="+ep)
	f.rec("UpdatePeerEndpoint:" + k)
	return nil
}
func (f *fakeTunnel) PeerReachability(string) (time.Time, uint64, uint64, error) {
	return time.Time{}, 0, 0, nil
}

type rosterFixture struct {
	h       *rosterHandler
	tun     *fakeTunnel
	sig     *recoverySignal
	evs     <-chan events.Event
	flipped *[]string
}

func newRosterFixture(t *testing.T) *rosterFixture {
	t.Helper()
	tun := &fakeTunnel{}
	bus := events.NewBus()
	evs, unsub := bus.Subscribe()
	t.Cleanup(func() { unsub(); bus.Close() })
	h := newRosterHandler(tun, peerstate.New(), bus, nil)
	sig := &recoverySignal{}
	h.client = sig
	var flipped []string
	h.setFlipPeerToDERPFn(func(k string) { flipped = append(flipped, k) })
	return &rosterFixture{h: h, tun: tun, sig: sig, evs: evs, flipped: &flipped}
}

func (f *rosterFixture) drain() []events.Event {
	var out []events.Event
	for {
		select {
		case e := <-f.evs:
			out = append(out, e)
		default:
			return out
		}
	}
}

func eventNodeIDs(evs []events.Event, typ events.EventType) []string {
	var out []string
	for _, e := range evs {
		if e.Type != typ {
			continue
		}
		if e.Peer != nil {
			out = append(out, e.Peer.NodeID)
		} else {
			out = append(out, e.NodeID)
		}
	}
	sort.Strings(out)
	return out
}

func storeIDs(h *rosterHandler) []string {
	var out []string
	for _, p := range h.ps.All() {
		out = append(out, p.NodeID)
	}
	sort.Strings(out)
	return out
}

func captureSlog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func wantStrings(t *testing.T, what string, got, want []string) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("%s = %v, want %v", what, got, want)
	}
}

func welcome(roster ...msignal.RosterPeer) msignal.Welcome {
	return msignal.Welcome{Type: "welcome", NodeID: "me", HouseholdID: "hh", MeshIP: selfIP, Roster: roster}
}

// mixedRoster has two good peers among every kind of bad entry.
func mixedRoster() []msignal.RosterPeer {
	badKey := rp("bad-key", "100.64.0.10", 7)
	badKey.WGPublicKey = "peerPubKeyABC"
	hostile := rp("hostile", "100.64.0.11\nallowed_ip=0.0.0.0/0", 8)
	return []msignal.RosterPeer{
		rp("linux", "100.64.0.7", 1),
		rp("gateway", "192.168.1.1", 2),
		rp("windows", "100.64.0.8", 3),
		rp("self-twin", selfIP, 4),
		rp("dup-a", "100.64.0.9", 5),
		rp("dup-b", "100.64.0.9", 6),
		badKey,
		hostile,
		rp("public", "8.8.8.8", 9),
		rp("prefixed", "100.64.0.12/32", 10),
	}
}

func TestOnWelcome_OnlyValidEntriesReachTunnelRoutesEventsAndStore(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)

	f.h.OnWelcome(welcome(mixedRoster()...))

	linux, windows := testKey(1), testKey(3)
	if f.tun.meshIP != selfIP {
		t.Errorf("SetMeshIP = %q", f.tun.meshIP)
	}
	if len(f.tun.setPeers) != 1 {
		t.Fatalf("SetPeers called %d times", len(f.tun.setPeers))
	}
	wantPeers := []tunnel.Peer{
		{PublicKey: linux, AllowedIPs: []string{"100.64.0.7/32"}, Endpoint: "203.0.113.1:51820", PersistentKeepalive: 25},
		{PublicKey: windows, AllowedIPs: []string{"100.64.0.8/32"}, Endpoint: "203.0.113.3:51820", PersistentKeepalive: 25},
	}
	if !reflect.DeepEqual(f.tun.setPeers[0], wantPeers) {
		t.Errorf("SetPeers got %+v, want %+v", f.tun.setPeers[0], wantPeers)
	}
	wantStrings(t, "routes added", f.tun.routesAdded, []string{"100.64.0.7", "100.64.0.8"})
	wantStrings(t, "DERP flips", *f.flipped, []string{linux, windows})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})

	evs := f.drain()
	wantStrings(t, "peer_added events", eventNodeIDs(evs, events.TypePeerAdded), []string{"linux", "windows"})

	// Only the valid peers (both have endpoints) get a coordinated punch.
	got := append([]string(nil), f.sig.requests...)
	sort.Strings(got)
	wantStrings(t, "request_punch", got, []string{"linux", "windows"})

	// One WARN per dropped entry; no key and no hostile text in the logs.
	out := logs.String()
	var dropped []string
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "roster: dropping peer") {
			dropped = append(dropped, line)
		}
	}
	if len(dropped) != 8 {
		t.Errorf("got %d drop lines, want 8:\n%s", len(dropped), out)
	}
	for _, id := range []string{"gateway", "self-twin", "dup-a", "dup-b", "bad-key", "hostile", "public", "prefixed"} {
		if !strings.Contains(out, "node_id="+id+" ") {
			t.Errorf("no drop line for %s", id)
		}
	}
	for _, leak := range []string{"allowed_ip", testKey(7), testKey(8), "peerPubKeyABC"} {
		if strings.Contains(out, leak) {
			t.Errorf("log output leaks %q", leak)
		}
	}
}

// A roster with nothing wrong in it must produce exactly the calls it did
// before validation existed.
func TestOnWelcome_ValidRosterBehaviourUnchanged(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)
	linux, windows := rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)

	f.h.OnWelcome(welcome(linux, windows))

	wantOps := []string{
		"SetMeshIP:" + selfIP,
		"SetPeers:2",
		"AddPeerRoute:100.64.0.7",
		"AddPeerRoute:100.64.0.8",
	}
	wantStrings(t, "ops", f.tun.ops, wantOps)
	if want := rosterToPeers([]msignal.RosterPeer{linux, windows}); !reflect.DeepEqual(f.tun.setPeers[0], want) {
		t.Errorf("SetPeers got %+v, want %+v", f.tun.setPeers[0], want)
	}
	wantStrings(t, "DERP flips", *f.flipped, []string{linux.WGPublicKey, windows.WGPublicKey})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})
	if strings.Contains(logs.String(), "level=WARN") {
		t.Errorf("valid roster logged a warning:\n%s", logs.String())
	}
}

func TestOnWelcome_AllEntriesRejectedStillClearsThePeerSet(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1)))
	f.h.OnWelcome(welcome(rp("linux", "192.168.1.1", 1)))

	if n := len(f.tun.setPeers); n != 2 || len(f.tun.setPeers[1]) != 0 {
		t.Fatalf("SetPeers calls = %v", f.tun.setPeers)
	}
	wantStrings(t, "store", storeIDs(f.h), nil)
}

func TestOnWelcome_PeerThatBecomesInvalidOnReconnectLosesPeerRouteAndListing(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))
	f.drain()
	f.tun.ops = nil

	// The coordinator now reports linux at a LAN gateway address.
	f.h.OnWelcome(welcome(rp("linux", "192.168.1.1", 1), rp("windows", "100.64.0.8", 3)))

	last := f.tun.setPeers[len(f.tun.setPeers)-1]
	if len(last) != 1 || last[0].PublicKey != testKey(3) {
		t.Fatalf("second SetPeers = %+v, want only windows", last)
	}
	wantStrings(t, "routes removed", f.tun.routesRemoved, []string{"100.64.0.7"})
	for _, ip := range f.tun.routesAdded {
		if ip == "192.168.1.1" {
			t.Fatal("a route was installed for the rejected address")
		}
	}
	// The stale route goes before the routes for the kept peers are installed.
	wantOps := []string{"SetMeshIP:" + selfIP, "RemovePeerRoute:100.64.0.7", "SetPeers:1", "AddPeerRoute:100.64.0.8"}
	wantStrings(t, "ops", f.tun.ops, wantOps)
	wantStrings(t, "store", storeIDs(f.h), []string{"windows"})
}

func TestOnWelcome_DuplicateAddressConfiguresNeither(t *testing.T) {
	captureSlog(t)
	for name, roster := range map[string][]msignal.RosterPeer{
		"a then b": {rp("a", "100.64.0.9", 1), rp("b", "100.64.0.9", 2), rp("ok", "100.64.0.20", 3)},
		"b then a": {rp("b", "100.64.0.9", 2), rp("a", "100.64.0.9", 1), rp("ok", "100.64.0.20", 3)},
	} {
		t.Run(name, func(t *testing.T) {
			f := newRosterFixture(t)
			f.h.OnWelcome(welcome(roster...))
			if len(f.tun.setPeers[0]) != 1 || f.tun.setPeers[0][0].PublicKey != testKey(3) {
				t.Fatalf("SetPeers = %+v, want only the unshared peer", f.tun.setPeers[0])
			}
			wantStrings(t, "routes added", f.tun.routesAdded, []string{"100.64.0.20"})
			wantStrings(t, "store", storeIDs(f.h), []string{"ok"})
		})
	}
}

func TestOnRosterUpdate_AddedMixedOnlyValidEntriesConfigured(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome())
	f.drain()
	f.tun.ops = nil

	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{
		rp("linux", "100.64.0.7", 1),
		rp("gateway", "192.168.1.1", 2),
		rp("self-twin", selfIP, 4),
		rp("dup-a", "100.64.0.9", 5),
		rp("dup-b", "100.64.0.9", 6),
		rp("windows", "100.64.0.8", 3),
	}})

	var keys []string
	for _, p := range f.tun.added {
		keys = append(keys, p.PublicKey)
	}
	wantStrings(t, "AddPeer", keys, []string{testKey(1), testKey(3)})
	wantStrings(t, "routes added", f.tun.routesAdded, []string{"100.64.0.7", "100.64.0.8"})
	wantStrings(t, "DERP flips", *f.flipped, []string{testKey(1), testKey(3)})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})
	wantStrings(t, "peer_added events", eventNodeIDs(f.drain(), events.TypePeerAdded), []string{"linux", "windows"})
	if len(f.tun.removedKeys) != 0 || len(f.tun.routesRemoved) != 0 {
		t.Errorf("a never-stored entry caused a teardown: %v %v", f.tun.removedKeys, f.tun.routesRemoved)
	}
}

// A peer that was valid, then arrives invalid in a
// roster update, must stop being a tunnel peer, lose its route, and stop being
// listed. Covered for both ways an update can carry it.
func TestOnRosterUpdate_PeerThatBecomesInvalidIsTornDown(t *testing.T) {
	for _, via := range []string{"updated", "added (rejoin)"} {
		t.Run(via, func(t *testing.T) {
			logs := captureSlog(t)
			f := newRosterFixture(t)
			f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))
			f.drain()
			*f.flipped = nil
			f.tun.ops = nil
			f.tun.endpointCalls = nil

			bad := rp("linux", "192.168.1.1", 1)
			var u msignal.RosterUpdate
			if via == "updated" {
				u.Updated = []msignal.RosterPeer{bad}
			} else {
				u.Added = []msignal.RosterPeer{bad}
			}
			f.h.OnRosterUpdate(u)

			wantOps := []string{"RemovePeer:" + testKey(1), "RemovePeerRoute:100.64.0.7"}
			wantStrings(t, "ops", f.tun.ops, wantOps)
			wantStrings(t, "store", storeIDs(f.h), []string{"windows"})
			if _, ok := f.h.ps.Get("linux"); ok {
				t.Error("linux is still in the store")
			}
			wantStrings(t, "peer_removed events", eventNodeIDs(f.drain(), events.TypePeerRemoved), []string{"linux"})
			if len(*f.flipped) != 0 || len(f.tun.endpointCalls) != 0 {
				t.Errorf("the rejected entry was acted on: flips=%v endpoints=%v", *f.flipped, f.tun.endpointCalls)
			}
			if strings.Count(logs.String(), "roster: dropping peer") != 1 {
				t.Errorf("want exactly one drop line:\n%s", logs.String())
			}
			if strings.Contains(logs.String(), "unknown node_id") {
				t.Errorf("the eviction was reported as an unknown removal:\n%s", logs.String())
			}
		})
	}
}

func TestOnRosterUpdate_ClaimOnAnotherPeersAddressDropsBoth(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))
	f.drain()
	f.tun.ops = nil

	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{rp("thief", "100.64.0.7", 9)}})

	// The claimant is never configured; the peer that held the address is
	// removed, so no key is left able to receive that address's traffic.
	wantOps := []string{"RemovePeer:" + testKey(1), "RemovePeerRoute:100.64.0.7"}
	wantStrings(t, "ops", f.tun.ops, wantOps)
	wantStrings(t, "store", storeIDs(f.h), []string{"windows"})
	wantStrings(t, "peer_removed events", eventNodeIDs(f.drain(), events.TypePeerRemoved), []string{"linux"})
}

func TestOnRosterUpdate_OwnAddressIsRefused(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome())
	f.tun.ops = nil
	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{rp("self-twin", selfIP, 4)}})
	wantStrings(t, "ops", f.tun.ops, nil)
	wantStrings(t, "store", storeIDs(f.h), nil)
}

func TestOnRosterUpdate_ExplicitRemovalAndEvictionOfSamePeerRunOnce(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1)))
	f.tun.ops = nil

	// The coordinator reassigned linux's address to a new node in the same
	// message that removes linux (the reconcile loop builds exactly this).
	f.h.OnRosterUpdate(msignal.RosterUpdate{
		Added:   []msignal.RosterPeer{rp("newcomer", "100.64.0.7", 9)},
		Removed: []string{"linux"},
	})

	wantStrings(t, "ops", f.tun.ops, []string{"RemovePeer:" + testKey(1), "RemovePeerRoute:100.64.0.7"})
	if strings.Contains(logs.String(), "unknown node_id") {
		t.Errorf("duplicate removal:\n%s", logs.String())
	}
	wantStrings(t, "store", storeIDs(f.h), nil)
}

// The ordinary update paths keep the call sequence they had.
func TestOnRosterUpdate_ValidUpdatesBehaviourUnchanged(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1)))
	f.tun.ops = nil
	*f.flipped = nil

	// New peer.
	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{rp("windows", "100.64.0.8", 3)}})
	wantStrings(t, "new peer ops", f.tun.ops, []string{
		"AddPeer:" + testKey(3) + ":100.64.0.8/32",
		"AddPeerRoute:100.64.0.8",
	})

	// Same-key rejoin of a known peer: clean re-sync, remove then add.
	f.tun.ops = nil
	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{rp("linux", "100.64.0.7", 1)}})
	wantStrings(t, "rejoin ops", f.tun.ops, []string{
		"RemovePeer:" + testKey(1),
		"AddPeer:" + testKey(1) + ":100.64.0.7/32",
		"AddPeerRoute:100.64.0.7",
	})

	// Endpoint update.
	f.tun.ops = nil
	moved := rp("windows", "100.64.0.8", 3)
	ep := "198.51.100.4:40000"
	moved.LastEndpoint = &ep
	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{moved}})
	wantStrings(t, "endpoint ops", f.tun.ops, []string{"UpdatePeerEndpoint:" + testKey(3)})
	if got, _ := f.h.ps.Get("windows"); got.LastEndpoint == nil || *got.LastEndpoint != ep {
		t.Error("updated endpoint not cached")
	}

	// Removal.
	f.tun.ops = nil
	f.h.OnRosterUpdate(msignal.RosterUpdate{Removed: []string{"windows"}})
	wantStrings(t, "removal ops", f.tun.ops, []string{"RemovePeer:" + testKey(3), "RemovePeerRoute:100.64.0.8"})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux"})

	if strings.Contains(logs.String(), "level=WARN") {
		t.Errorf("valid updates logged a warning:\n%s", logs.String())
	}
}

// An Updated entry for a node the store did not hold has never been configured
// on the tunnel, so it has to be set up like a new peer, not just given an
// endpoint. wantNewPeerSetup checks that: one AddPeer with the node's /32, its
// host route, a DERP flip, a peer_added event (and no peer_updated), a punch
// request, no teardown and no endpoint-only call. The recorders must have been
// reset (resetRecorders) just before the update under test.
func wantNewPeerSetup(t *testing.T, f *rosterFixture, id, ip string, seed byte) {
	t.Helper()
	wantStrings(t, "ops", f.tun.ops, []string{
		"AddPeer:" + testKey(seed) + ":" + ip + "/32",
		"AddPeerRoute:" + ip,
	})
	if len(f.tun.endpointCalls) != 0 || len(f.tun.removedKeys) != 0 {
		t.Errorf("endpoint-only or teardown call for a new peer: endpoints=%v removed=%v", f.tun.endpointCalls, f.tun.removedKeys)
	}
	wantStrings(t, "DERP flips", *f.flipped, []string{testKey(seed)})
	evs := f.drain()
	wantStrings(t, "peer_added events", eventNodeIDs(evs, events.TypePeerAdded), []string{id})
	wantStrings(t, "peer_updated events", eventNodeIDs(evs, events.TypePeerUpdated), nil)
	wantStrings(t, "punch requests", f.sig.requests, []string{id})
	if _, ok := f.h.ps.Get(id); !ok {
		t.Errorf("%s is not in the store", id)
	}
}

func (f *rosterFixture) resetRecorders() {
	f.tun.ops = nil
	f.tun.endpointCalls = nil
	f.tun.removedKeys = nil
	*f.flipped = nil
	f.sig.requests = nil
	f.drain()
}

// 1. A held peer is evicted by an invalid update, then a valid Updated entry
// for it arrives while the coordinator connection stays up. The store no longer
// holds it, so it must be added again, not given an endpoint on a tunnel that
// no longer has the peer.
func TestOnRosterUpdate_UpdatedEntryAfterEvictionIsAddedBack(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))

	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{rp("linux", "192.168.1.1", 1)}})
	wantStrings(t, "store after eviction", storeIDs(f.h), []string{"windows"})
	f.resetRecorders()

	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{rp("linux", "100.64.0.7", 1)}})

	wantNewPeerSetup(t, f, "linux", "100.64.0.7", 1)
	if got, ok := f.h.ps.Get("linux"); !ok || got.MeshIP != "100.64.0.7" {
		t.Errorf("store entry = %+v, %v", got, ok)
	}
	if strings.Contains(logs.String(), "same-key rejoin") {
		t.Errorf("a new peer was handled as a rejoin:\n%s", logs.String())
	}
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})
	if len(f.tun.added) != 1 {
		t.Errorf("AddPeer calls = %d, want 1", len(f.tun.added))
	}
}

// 2. An Updated entry for a node the store never held (its "added" was
// missed): same result as a new peer.
func TestOnRosterUpdate_UpdatedEntryForNeverHeldNodeIsAdded(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("windows", "100.64.0.8", 3)))
	f.resetRecorders()

	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{rp("linux", "100.64.0.7", 1)}})

	wantStrings(t, "ops", f.tun.ops, []string{
		"AddPeer:" + testKey(1) + ":100.64.0.7/32",
		"AddPeerRoute:100.64.0.7",
	})
	if len(f.tun.endpointCalls) != 0 || len(f.tun.removedKeys) != 0 {
		t.Errorf("endpoint-only or teardown call: endpoints=%v removed=%v", f.tun.endpointCalls, f.tun.removedKeys)
	}
	wantStrings(t, "DERP flips", *f.flipped, []string{testKey(1)})
	evs := f.drain()
	wantStrings(t, "peer_added events", eventNodeIDs(evs, events.TypePeerAdded), []string{"linux"})
	wantStrings(t, "peer_updated events", eventNodeIDs(evs, events.TypePeerUpdated), nil)
	wantStrings(t, "punch requests", f.sig.requests, []string{"linux"})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})
	if len(f.tun.added) != 1 || !reflect.DeepEqual(f.tun.added[0].AllowedIPs, []string{"100.64.0.7/32"}) {
		t.Errorf("AddPeer calls = %+v", f.tun.added)
	}
}

// 3. An Updated entry for a held node with a new endpoint is one endpoint
// update, nothing else, with the full call sequence pinned.
func TestOnRosterUpdate_UpdatedEntryForHeldNodeIsEndpointOnly(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1), rp("windows", "100.64.0.8", 3)))
	f.resetRecorders()

	moved := rp("linux", "100.64.0.7", 1)
	ep := "198.51.100.4:40000"
	moved.LastEndpoint = &ep
	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{moved}})

	wantStrings(t, "ops", f.tun.ops, []string{"UpdatePeerEndpoint:" + testKey(1)})
	wantStrings(t, "endpoint calls", f.tun.endpointCalls, []string{testKey(1) + "=" + ep})
	if len(f.tun.added) != 0 {
		t.Errorf("AddPeer called for a held node: %+v", f.tun.added)
	}
	if len(*f.flipped) != 0 {
		t.Errorf("held node was flipped again: %v", *f.flipped)
	}
	evs := f.drain()
	wantStrings(t, "peer_updated events", eventNodeIDs(evs, events.TypePeerUpdated), []string{"linux"})
	wantStrings(t, "peer_added events", eventNodeIDs(evs, events.TypePeerAdded), nil)
	wantStrings(t, "store", storeIDs(f.h), []string{"linux", "windows"})
	if got, _ := f.h.ps.Get("linux"); got.LastEndpoint == nil || *got.LastEndpoint != ep {
		t.Error("updated endpoint not cached")
	}
}

// 4. The same new node in Added and in Updated of one message is added once,
// from the later entry (the one the store keeps). Twice in Updated: once too.
// Held node in both lists: rejoin re-sync, then the endpoint update.
func TestOnRosterUpdate_SameNodeInAddedAndUpdatedIsAddedOnce(t *testing.T) {
	later := rp("linux", "100.64.0.7", 1)
	laterEp := "198.51.100.9:41000"
	later.LastEndpoint = &laterEp
	first := rp("linux", "100.64.0.7", 1)

	t.Run("added and updated, not held", func(t *testing.T) {
		logs := captureSlog(t)
		f := newRosterFixture(t)
		f.h.OnWelcome(welcome())
		f.resetRecorders()

		f.h.OnRosterUpdate(msignal.RosterUpdate{
			Added:   []msignal.RosterPeer{first},
			Updated: []msignal.RosterPeer{later},
		})

		wantNewPeerSetup(t, f, "linux", "100.64.0.7", 1)
		if len(f.tun.added) != 1 || f.tun.added[0].Endpoint != laterEp {
			t.Errorf("AddPeer = %+v, want one call with endpoint %s", f.tun.added, laterEp)
		}
		if got, _ := f.h.ps.Get("linux"); got.LastEndpoint == nil || *got.LastEndpoint != laterEp {
			t.Errorf("store endpoint = %v, want %s", got.LastEndpoint, laterEp)
		}
		if strings.Contains(logs.String(), "same-key rejoin") {
			t.Errorf("handled as a rejoin:\n%s", logs.String())
		}
	})

	t.Run("updated twice, not held", func(t *testing.T) {
		captureSlog(t)
		f := newRosterFixture(t)
		f.h.OnWelcome(welcome())
		f.resetRecorders()

		f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{first, later}})

		wantNewPeerSetup(t, f, "linux", "100.64.0.7", 1)
		if len(f.tun.added) != 1 || f.tun.added[0].Endpoint != laterEp {
			t.Errorf("AddPeer = %+v, want one call with endpoint %s", f.tun.added, laterEp)
		}
	})

	t.Run("added and updated, held (unchanged behaviour)", func(t *testing.T) {
		captureSlog(t)
		f := newRosterFixture(t)
		f.h.OnWelcome(welcome(rp("linux", "100.64.0.7", 1)))
		f.resetRecorders()

		f.h.OnRosterUpdate(msignal.RosterUpdate{
			Added:   []msignal.RosterPeer{first},
			Updated: []msignal.RosterPeer{later},
		})

		wantStrings(t, "ops", f.tun.ops, []string{
			"RemovePeer:" + testKey(1),
			"AddPeer:" + testKey(1) + ":100.64.0.7/32",
			"AddPeerRoute:100.64.0.7",
			"UpdatePeerEndpoint:" + testKey(1),
		})
	})
}

// 5. An Updated entry that is refused for a node that was not held: nothing is
// configured, nothing is stored, nothing is published, and the drop is logged
// once.
func TestOnRosterUpdate_RefusedUpdatedEntryForNeverHeldNodeDoesNothing(t *testing.T) {
	logs := captureSlog(t)
	f := newRosterFixture(t)
	f.h.OnWelcome(welcome(rp("windows", "100.64.0.8", 3)))
	f.resetRecorders()

	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{rp("linux", "192.168.1.1", 1)}})

	wantStrings(t, "ops", f.tun.ops, nil)
	wantStrings(t, "store", storeIDs(f.h), []string{"windows"})
	if evs := f.drain(); len(evs) != 0 {
		t.Errorf("events published for a refused entry: %+v", evs)
	}
	if len(*f.flipped) != 0 || len(f.sig.requests) != 0 || len(f.tun.endpointCalls) != 0 {
		t.Errorf("refused entry was acted on: flips=%v punches=%v endpoints=%v", *f.flipped, f.sig.requests, f.tun.endpointCalls)
	}
	if strings.Count(logs.String(), "roster: dropping peer") != 1 {
		t.Errorf("want exactly one drop line:\n%s", logs.String())
	}
}

// The reconcile loop is the other way roster entries arrive: a GET /mesh/peers
// response is diffed against the cache and fed to OnRosterUpdate.
func TestReconcileRosterOnce_DropsInvalidEntriesFromCoordinatorResponse(t *testing.T) {
	logs := captureSlog(t)
	myKey := testKey(0x55)
	var mu sync.Mutex
	response := []coordapi.EnrolledPeer{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(response)
	}))
	defer srv.Close()
	client := coordapi.New(srv.URL)

	f := newRosterFixture(t)
	f.h.OnWelcome(welcome())
	f.tun.ops = nil
	state := localapi.NewState()
	state.SetWGPublicKey(myKey)

	enrolled := func(p msignal.RosterPeer) coordapi.EnrolledPeer {
		return coordapi.EnrolledPeer{NodeID: p.NodeID, NodeName: p.NodeName, MeshIP: p.MeshIP, WGPublicKey: p.WGPublicKey, LastEndpoint: p.LastEndpoint}
	}
	self := rp("me", selfIP, 0x55)
	mu.Lock()
	response = []coordapi.EnrolledPeer{
		enrolled(self), // the coordinator includes the caller; reconcile skips it by key
		enrolled(rp("linux", "100.64.0.7", 1)),
		enrolled(rp("gateway", "192.168.1.1", 2)),
		enrolled(rp("impostor", selfIP, 4)), // a different key claiming this node's address
		enrolled(rp("dup-a", "100.64.0.9", 5)),
		enrolled(rp("dup-b", "100.64.0.9", 6)),
	}
	mu.Unlock()

	reconcileRosterOnce(context.Background(), client, f.h.ps, f.h, state)

	var keys []string
	for _, p := range f.tun.added {
		keys = append(keys, p.PublicKey)
	}
	wantStrings(t, "AddPeer", keys, []string{testKey(1)})
	wantStrings(t, "routes added", f.tun.routesAdded, []string{"100.64.0.7"})
	wantStrings(t, "store", storeIDs(f.h), []string{"linux"})
	if n := strings.Count(logs.String(), "roster: dropping peer"); n != 4 {
		t.Errorf("want 4 drop lines, got %d:\n%s", n, logs.String())
	}

	// The next cycle sees the same response. Nothing valid changes, so only the
	// refused entries are judged (and logged) again; the valid peer is untouched.
	f.tun.ops = nil
	reconcileRosterOnce(context.Background(), client, f.h.ps, f.h, state)
	for _, op := range f.tun.ops {
		if strings.Contains(op, testKey(1)) {
			t.Errorf("valid peer was touched on the next cycle: %v", f.tun.ops)
		}
	}
	wantStrings(t, "store after second cycle", storeIDs(f.h), []string{"linux"})
}
