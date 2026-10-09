package circlelink

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/netip"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

func TestMain(m *testing.M) {
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	os.Exit(m.Run())
}

// ---------------------------------------------------------------- identities

const (
	selfIP  = "100.64.0.6" // this node
	hhIP    = "100.64.0.7" // its household partner
	linkA   = "c0ffee00-1111-4222-8333-444455556666"
	linkB   = "d00dfeed-1111-4222-8333-444455556666"
	linkC   = "0badcafe-1111-4222-8333-444455556666"
	tokenA  = "45827150f595bab19278b82d22eba203d126486c59732655abb65ea67d209ee6"
	tokenB  = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
	circIP1 = "100.64.1.2"
	circIP2 = "100.64.2.2"
	wanEP   = "203.0.113.9:51820"
)

// keyN returns a canonical WireGuard public key (base64 of 32 bytes, all n).
func keyN(n byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = n
	}
	return base64.StdEncoding.EncodeToString(b)
}

var selfKey = keyN(0xEE)

func sp(s string) *string { return &s }

// ---------------------------------------------------------------- fakes

// opLog is one ordered log of everything the fakes were asked to do, so a test
// can assert the order across the tunnel, the filter, the relay and the signal.
type opLog struct {
	mu  sync.Mutex
	ops []string
}

func (l *opLog) add(format string, a ...any) {
	l.mu.Lock()
	l.ops = append(l.ops, fmt.Sprintf(format, a...))
	l.mu.Unlock()
}
func (l *opLog) list() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.ops...)
}
func (l *opLog) reset() { l.mu.Lock(); l.ops = nil; l.mu.Unlock() }
func (l *opLog) String() string {
	return strings.Join(l.list(), " | ")
}

// names makes the log readable: keys print as k<byte> and addresses as given.
func name(key string) string {
	b, err := base64.StdEncoding.DecodeString(key)
	if err != nil || len(b) != 32 {
		return "badkey"
	}
	return fmt.Sprintf("k%02x", b[0])
}

type fakeTun struct {
	log *opLog

	mu       sync.Mutex
	peers    map[string]tunnel.Peer
	routes   map[string]bool
	fail     map[string]error // op -> error, every call
	hs       map[string]time.Time
	liveEP   map[string]string
	removeOK bool // RemovePeer succeeds only when true while failRemove is set
}

func newFakeTun(log *opLog) *fakeTun {
	return &fakeTun{log: log, peers: map[string]tunnel.Peer{}, routes: map[string]bool{}, fail: map[string]error{}, hs: map[string]time.Time{}, liveEP: map[string]string{}}
}

func (f *fakeTun) failOn(op string, err error) { f.mu.Lock(); f.fail[op] = err; f.mu.Unlock() }
func (f *fakeTun) clearFail(op string)         { f.mu.Lock(); delete(f.fail, op); f.mu.Unlock() }
func (f *fakeTun) setHandshake(key string, t time.Time) {
	f.mu.Lock()
	f.hs[key] = t
	f.mu.Unlock()
}
func (f *fakeTun) hasPeer(key string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.peers[key]
	return ok
}
func (f *fakeTun) peer(key string) tunnel.Peer {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.peers[key]
}
func (f *fakeTun) hasRoute(ip string) bool { f.mu.Lock(); defer f.mu.Unlock(); return f.routes[ip] }

func (f *fakeTun) AddPeer(p tunnel.Peer) error {
	f.log.add("AddPeer %s ep=%s ka=%d", name(p.PublicKey), p.Endpoint, p.PersistentKeepalive)
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail["AddPeer"]; err != nil {
		return err
	}
	f.peers[p.PublicKey] = p
	return nil
}
func (f *fakeTun) RemovePeer(key string) error {
	f.log.add("RemovePeer %s", name(key))
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail["RemovePeer"]; err != nil {
		return err
	}
	delete(f.peers, key)
	delete(f.hs, key)
	return nil
}
func (f *fakeTun) AddPeerRoute(ip string) error {
	f.log.add("AddPeerRoute %s", ip)
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail["AddPeerRoute"]; err != nil {
		return err
	}
	f.routes[ip] = true
	return nil
}
func (f *fakeTun) RemovePeerRoute(ip string) error {
	f.log.add("RemovePeerRoute %s", ip)
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail["RemovePeerRoute"]; err != nil {
		return err
	}
	delete(f.routes, ip)
	return nil
}
func (f *fakeTun) PeerReachability(key string) (time.Time, uint64, uint64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.hs[key], 0, 0, nil
}
func (f *fakeTun) PeerEndpoint(key string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if ep, ok := f.liveEP[key]; ok {
		return ep, nil
	}
	return f.peers[key].Endpoint, nil
}

// fakeFilter keeps the entry table the real filter would, without packets.
type fakeFilter struct {
	log *opLog

	mu      sync.Mutex
	entries map[netip.Addr]tunnel.Grant
	fail    error
}

func newFakeFilter(log *opLog) *fakeFilter {
	return &fakeFilter{log: log, entries: map[netip.Addr]tunnel.Grant{}}
}
func (f *fakeFilter) SetEntry(a netip.Addr, g tunnel.Grant) error {
	f.log.add("SetEntry %s in=%d out=%d ttl=%s", a, g.InPort, g.OutPort, g.TTL.Round(time.Second))
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return f.fail
	}
	f.entries[a] = g
	return nil
}
func (f *fakeFilter) RemoveEntry(a netip.Addr) {
	f.log.add("RemoveEntry %s", a)
	f.mu.Lock()
	delete(f.entries, a)
	f.mu.Unlock()
}
func (f *fakeFilter) entry(a string) (tunnel.Grant, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	g, ok := f.entries[netip.MustParseAddr(a)]
	return g, ok
}
func (f *fakeFilter) count() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.entries) }

type fakeRelay struct {
	log *opLog
	mu  sync.Mutex
	on  map[[32]byte]bool
}

func newFakeRelay(log *opLog) *fakeRelay { return &fakeRelay{log: log, on: map[[32]byte]bool{}} }
func (r *fakeRelay) SetPeerRouting(pub [32]byte, via bool) {
	r.log.add("SetPeerRouting %02x %v", pub[0], via)
	r.mu.Lock()
	if via {
		r.on[pub] = true
	} else {
		delete(r.on, pub)
	}
	r.mu.Unlock()
}
func (r *fakeRelay) routed(key string) bool {
	b, _ := base64.StdEncoding.DecodeString(key)
	var pub [32]byte
	copy(pub[:], b)
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.on[pub]
}

type fakeSignal struct {
	log  *opLog
	mu   sync.Mutex
	acks []signal.CirclePeerAck
}

func (s *fakeSignal) SendCirclePeerAck(a signal.CirclePeerAck) {
	reason := "-"
	if a.Reason != nil {
		reason = *a.Reason
	}
	s.log.add("ack %s %s %s", a.Op, a.State, reason)
	s.mu.Lock()
	s.acks = append(s.acks, a)
	s.mu.Unlock()
}
func (s *fakeSignal) last() signal.CirclePeerAck {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.acks) == 0 {
		return signal.CirclePeerAck{}
	}
	return s.acks[len(s.acks)-1]
}
func (s *fakeSignal) count() int { s.mu.Lock(); defer s.mu.Unlock(); return len(s.acks) }

// fakeClock moves the two clocks separately, so a test can simulate a machine
// that slept (wall moves, monotonic does not).
type fakeClock struct {
	mu         sync.Mutex
	mono, wall int64
}

func newFakeClock() *fakeClock {
	return &fakeClock{mono: int64(time.Hour), wall: time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC).UnixNano()}
}
func (c *fakeClock) read() (int64, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mono, c.wall
}
func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	c.mono += int64(d)
	c.wall += int64(d)
	c.mu.Unlock()
}

// sleep moves only the wall clock: the machine was suspended.
func (c *fakeClock) sleep(d time.Duration) { c.mu.Lock(); c.wall += int64(d); c.mu.Unlock() }
func (c *fakeClock) wallTime() time.Time   { _, w := c.read(); return time.Unix(0, w) }

// ---------------------------------------------------------------- environment

type env struct {
	t     *testing.T
	c     *Controller
	log   *opLog
	tun   *fakeTun
	flt   *fakeFilter
	rel   *fakeRelay
	sig   *fakeSignal
	hh    *peerstate.Store
	clk   *fakeClock
	self  netip.Addr
	skip  bool
	lanOK func([]string) (string, bool)
}

type envOpt func(*envCfg)
type envCfg struct {
	relay      bool
	skipDirect func() bool
	pickLAN    func([]string) (string, bool)
	noFilter   bool
}

func withoutRelay() envOpt { return func(c *envCfg) { c.relay = false } }
func withSkipDirect(f func() bool) envOpt {
	return func(c *envCfg) { c.skipDirect = f }
}
func withPickLAN(f func([]string) (string, bool)) envOpt {
	return func(c *envCfg) { c.pickLAN = f }
}
func withoutFilter() envOpt { return func(c *envCfg) { c.noFilter = true } }

// newEnv builds a controller for a node at selfIP whose household has one other
// machine at hhIP.
func newEnv(t *testing.T, opts ...envOpt) *env {
	t.Helper()
	cfg := &envCfg{relay: true}
	for _, o := range opts {
		o(cfg)
	}
	log := &opLog{}
	e := &env{t: t, log: log, tun: newFakeTun(log), flt: newFakeFilter(log), rel: newFakeRelay(log), sig: &fakeSignal{log: log}, clk: newFakeClock(), hh: peerstate.New()}
	e.self = netip.MustParseAddr(selfIP)
	_, rej := e.hh.Reset(selfIP, []signal.RosterPeer{{NodeID: "n-b", NodeName: "node-b", MeshIP: hhIP, WGPublicKey: keyN(0x0B)}})
	if len(rej) != 0 {
		t.Fatalf("household setup rejected: %v", rej)
	}
	cc := Config{
		Tunnel:     e.tun,
		Signal:     e.sig,
		Household:  e.hh,
		Self:       func() (netip.Addr, string) { return e.self, selfKey },
		Now:        e.clk.read,
		SkipDirect: cfg.skipDirect,
		PickLAN:    cfg.pickLAN,
	}
	if !cfg.noFilter {
		cc.Filter = e.flt
	}
	if cfg.relay {
		cc.Relay = e.rel
	}
	e.c = New(cc)
	return e
}

// addMsg builds a circle_peer_add the way the fixture's examples do.
func addMsg(link, role, key, ip string, port, ttl int) signal.CirclePeerAdd {
	m := signal.CirclePeerAdd{
		Type: "circle_peer_add", LinkID: link, Role: role, PeerWGPublicKey: key, PeerMeshIP: ip,
		Port: port, TTLSeconds: ttl, PeerLocalEndpoints: []string{}, DirectWindowMS: 0,
	}
	if role == signal.CircleRoleLender {
		m.TokenSHA256 = sp(tokenA)
	}
	return m
}

// withDirect turns an add into the direct policy: a public endpoint and a window.
func withDirect(m signal.CirclePeerAdd, ep string, window int) signal.CirclePeerAdd {
	m.PeerEndpoint = sp(ep)
	m.DirectWindowMS = window
	return m
}

func (e *env) add(m signal.CirclePeerAdd) signal.CirclePeerAck {
	e.t.Helper()
	before := e.sig.count()
	e.c.OnCirclePeerAdd(m)
	if e.sig.count() != before+1 {
		e.t.Fatalf("add for %s: expected exactly one ack, got %d", m.LinkID, e.sig.count()-before)
	}
	return e.sig.last()
}

func (e *env) extend(link string, ttl int, ep *string) signal.CirclePeerAck {
	e.t.Helper()
	before := e.sig.count()
	e.c.OnCirclePeerExtend(signal.CirclePeerExtend{Type: "circle_peer_extend", LinkID: link, TTLSeconds: ttl, PeerEndpoint: ep})
	if e.sig.count() != before+1 {
		e.t.Fatalf("extend for %s: expected exactly one ack, got %d", link, e.sig.count()-before)
	}
	return e.sig.last()
}

func (e *env) remove(link, reason string) {
	e.t.Helper()
	before := e.sig.count()
	e.c.OnCirclePeerRemove(signal.CirclePeerRemove{Type: "circle_peer_remove", LinkID: link, Reason: reason})
	if e.sig.count() != before {
		e.t.Fatalf("remove must not be acked")
	}
}

func mustReady(t *testing.T, a signal.CirclePeerAck, op string) {
	t.Helper()
	if a.State != signal.CircleStateReady || a.Reason != nil || a.Op != op {
		t.Fatalf("want ready %s ack, got %+v (reason %v)", op, a, deref(a.Reason))
	}
}

func mustReject(t *testing.T, a signal.CirclePeerAck, op, reason string) {
	t.Helper()
	if a.State != signal.CircleStateRejected || a.Reason == nil || *a.Reason != reason || a.Op != op {
		t.Fatalf("want rejected %s ack with %s, got %+v (reason %v)", op, reason, a, deref(a.Reason))
	}
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}

// nothingHeld asserts the controller, the filter, the tunnel and the relay hold
// nothing for a peer.
func (e *env) nothingHeld(key, ip string) {
	e.t.Helper()
	if _, ok := e.flt.entry(ip); ok {
		e.t.Errorf("filter entry for %s still present", ip)
	}
	if e.tun.hasPeer(key) {
		e.t.Errorf("wireguard peer %s still on the device", name(key))
	}
	if e.tun.hasRoute(ip) {
		e.t.Errorf("host route to %s still present", ip)
	}
	if e.rel.routed(key) {
		e.t.Errorf("relay flag for %s still set", name(key))
	}
	if n := len(e.c.Peers()); n != 0 {
		e.t.Errorf("controller still lists %d peers", n)
	}
	for _, p := range e.c.Kept() {
		e.t.Errorf("kept snapshot still carries %s", name(p.PublicKey))
	}
}

var errBoom = errors.New("boom")

// synthetic is the relay endpoint string for a key.
func synthetic(key string) string {
	b, _ := base64.StdEncoding.DecodeString(key)
	var pub [32]byte
	copy(pub[:], b)
	return derp.SyntheticEndpointString(pub)
}

func ctxWithCancel() (context.Context, context.CancelFunc) {
	return context.WithCancel(context.Background())
}

func netipZero() netip.Addr { return netip.Addr{} }
