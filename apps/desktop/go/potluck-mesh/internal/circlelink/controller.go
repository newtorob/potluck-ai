// Package circlelink holds the restricted WireGuard peers of direct circle
// links. A circle link joins two machines of different households for a short
// time: the coordinator brokers it, each sidecar adds the other machine as a
// peer that the packet filter confines to one TCP port, and the request then
// goes machine to machine.
//
// The Controller adds a peer when the coordinator says so, holds it, extends it,
// and removes it: on the coordinator's word, at the deadline on this machine's
// own clocks (whether or not the coordinator is reachable), when a household
// machine claims its address, and on unpair.
//
// A circle peer is never in the household store, so it never appears in
// GET /peers, in peers_online_count or on the event bus, and the household
// reachability, LAN-direct and punch-trial machinery never sees it. The
// packet filter treats an address with a filter entry as restricted whatever the
// household store says, so the controller installs the entry first when it adds
// a peer and removes it last when it removes one. If the controller or the
// tunnel misbehaves, the failure is a peer that is unreachable or an entry that
// outlives its WireGuard peer, never a peer that is open: the filter drops every
// address that has neither an entry nor a household record.
package circlelink

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"log/slog"
	"net/netip"
	"regexp"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
	"github.com/potluck-ai/potluck-mesh/internal/util"
)

const (
	// MaxPeers is how many circle peers one sidecar holds. A sixteenth machine is
	// more than any circle needs, and the cap bounds what a misbehaving
	// coordinator can fill the device with.
	MaxPeers = 16
	// MaxTTL caps how long this sidecar holds a link whatever the message says.
	MaxTTL = 30 * time.Minute
	// MaxDirectWindow caps how long a direct path is tried before the relay.
	MaxDirectWindow = 10 * time.Second

	maxLocalEndpoints = 4

	// probeKeepalive is the persistent keepalive, in seconds, while a direct path
	// is being tried. It makes WireGuard start a handshake at once. steadyKeepalive
	// is the household's value and keeps a relayed or confirmed link fresh.
	probeKeepalive  = 5
	steadyKeepalive = 25

	// pokeAfter is when a peer that has no handshake yet is re-added once. See
	// pokeLocked.
	pokeAfter = 1500 * time.Millisecond

	// lenderKickAfter is how long a machine that only lends waits before it starts
	// a handshake of its own. The requester starts one the moment the link is
	// added; if the lender started one at the same moment too, each side would
	// answer the other's initiation and drop its own state, and the pair would
	// sit idle until a retry (measured here: 1.5 to 2 seconds). The lender is
	// installed before the requester is told the link is ready, so the requester's
	// first packet finds it; this delay only covers the requester's add arriving a
	// few milliseconds ahead of the lender's.
	lenderKickAfter = 400 * time.Millisecond

	// FreshWindow is how old a handshake may be for the peer to count as
	// reachable. It is the same three minutes GET /peers uses: a healthy link
	// re-handshakes about every two minutes, so a shorter window would call a
	// working link dead.
	FreshWindow = 3 * time.Minute

	// staleCheckEvery spaces the look at a confirmed direct path's handshake age.
	staleCheckEvery = 5 * time.Second

	// maxStepFails bounds the retries of a transport step (poke or flip) that
	// failed in the tunnel.
	maxStepFails = 3

	// zombieTries bounds the retries of a peer removal that failed in the tunnel.
	zombieTries = 60

	fastTick = 100 * time.Millisecond
	slowTick = time.Second
)

// Tunnel is what the controller needs of the WireGuard device. *tunnel.Tunnel
// satisfies it.
type Tunnel interface {
	AddPeer(tunnel.Peer) error
	RemovePeer(pubkey string) error
	AddPeerRoute(meshIP string) error
	RemovePeerRoute(meshIP string) error
	PeerReachability(pubkey string) (time.Time, uint64, uint64, error)
	PeerEndpoint(pubkey string) (string, error)
}

// Filter is the packet filter's entry table. *tunnel.PacketFilter satisfies it.
type Filter interface {
	SetEntry(peer netip.Addr, g tunnel.Grant) error
	RemoveEntry(peer netip.Addr)
}

// Relay is the DERP bind's per-peer routing flag. *derp.DerpBind satisfies it. A
// nil Relay means no relay is configured.
type Relay interface {
	SetPeerRouting(pubkey [32]byte, viaDERP bool)
}

// Signal sends acks to the coordinator. *signal.Client satisfies it.
type Signal interface {
	SendCirclePeerAck(signal.CirclePeerAck)
}

// Household is the household roster as the controller needs it. *peerstate.Store
// satisfies it.
type Household interface {
	IsHousehold(a [4]byte) bool
	HasKey(key string) bool
	CheckMeshAddr(raw string, self netip.Addr) (netip.Addr, error)
}

// Clock returns two readings of now in nanoseconds: a monotonic one, which on
// most systems stops while the machine sleeps, and the wall clock. A grant is
// over when either has passed its deadline, so a laptop that slept past the
// deadline lets go on waking. It is the same rule the packet filter applies.
type Clock func() (mono, wall int64)

// Config wires a Controller to the rest of the sidecar.
type Config struct {
	Tunnel    Tunnel
	Filter    Filter
	Relay     Relay // nil when no relay is configured
	Signal    Signal
	Household Household

	// Self returns this node's mesh address (zero until the first welcome) and
	// its WireGuard public key (base64).
	Self func() (netip.Addr, string)

	// SkipDirect, when set and true, sends a link straight to the relay (this
	// node's NAT is symmetric, so a direct try would only burn the window). It is
	// ignored when no relay is configured.
	SkipDirect func() bool

	// PickLAN chooses among the private LAN candidates a message supplied. It
	// returns one that is on a subnet this host is on, or false. Nil means never
	// use a LAN candidate. The candidates come from the message and nowhere else.
	PickLAN func(candidates []string) (string, bool)

	// Now is the clock; nil means the real one. After is the timer the Run loop
	// waits on; nil means time.After.
	Now   Clock
	After func(time.Duration) <-chan time.Time
}

// transport is how a peer's packets leave this host.
type transport uint8

const (
	// tpNone: no endpoint and no relay. Nothing is sent; the peer is reachable
	// only if it reaches this host first.
	tpNone transport = iota
	// tpDirect: pinned to an endpoint, relay flag off.
	tpDirect
	// tpRelay: the synthetic relay endpoint, relay flag on.
	tpRelay
)

func (t transport) String() string {
	switch t {
	case tpDirect:
		return "direct"
	case tpRelay:
		return "derp"
	default:
		return "none"
	}
}

// grant is one link's rights on a peer, in one direction.
type grant struct {
	linkID       string
	port         uint16
	tokenSHA     string // lender role only
	monoDeadline int64
	wallDeadline int64
}

func (g *grant) expired(mono, wall int64) bool {
	return mono >= g.monoDeadline || wall >= g.wallDeadline
}

// remaining is the time left on the shorter of the two clocks.
func (g *grant) remaining(mono, wall int64) time.Duration {
	r := g.monoDeadline - mono
	if w := g.wallDeadline - wall; w < r {
		r = w
	}
	return time.Duration(r)
}

// peer is one WireGuard peer held for a circle link. It can carry two grants: A
// lends to B while B lends to A is one WireGuard peer on each side.
type peer struct {
	key  string
	pub  [32]byte
	addr netip.Addr

	inbound  *grant // the peer may call a port on this host (we lend)
	outbound *grant // this host may call a port on the peer (we borrow)

	tp        transport
	endpoint  string // the pinned endpoint while tp is tpDirect
	keepalive int

	addedWall   int64 // handshakes after this belong to this peer's current life
	windowEnd   int64 // mono; zero when no direct window is open
	pokeAt      int64 // mono; zero when no poke is pending
	kickAt      int64 // mono; when a lender-only peer turns its keepalive on and so starts a handshake
	kickAlive   int   // the keepalive to turn on then
	confirmed   bool  // a handshake was seen on the direct path
	nextStale   int64 // mono; when to look at the handshake age of a confirmed path
	stepFails   int
	routeAdded  bool
	relayMarked bool
}

// quietStart holds a lend-only peer's keepalive back for lenderKickAfter. It is
// used whenever the peer object is about to be created again (the first add, the
// move to the relay, the poke), so that the requester's handshake initiation goes
// first each time and the two sides do not start one at the same moment.
func (p *peer) quietStart(mono int64) {
	if p.outbound != nil || p.keepalive == 0 {
		return
	}
	p.kickAlive, p.keepalive = p.keepalive, 0
	p.kickAt = mono + int64(lenderKickAfter)
}

func (p *peer) grants() []*grant {
	var out []*grant
	if p.inbound != nil {
		out = append(out, p.inbound)
	}
	if p.outbound != nil {
		out = append(out, p.outbound)
	}
	return out
}

func (p *peer) grantFor(role string) **grant {
	if role == signal.CircleRoleLender {
		return &p.inbound
	}
	return &p.outbound
}

// tunnelPeer is the WireGuard configuration of the peer as it should be now.
func (p *peer) tunnelPeer() tunnel.Peer {
	ep := ""
	switch p.tp {
	case tpDirect:
		ep = p.endpoint
	case tpRelay:
		ep = derp.SyntheticEndpointString(p.pub)
	}
	return tunnel.Peer{
		PublicKey:           p.key,
		AllowedIPs:          []string{p.addr.String() + "/32"},
		Endpoint:            ep,
		PersistentKeepalive: p.keepalive,
		Private:             true,
	}
}

// linkRef says which peer and which side a link id belongs to. Only the grant
// that currently owns a peer's side has a ref: after a supersede the old link id
// has none, so a late remove for it finds nothing and changes nothing.
type linkRef struct {
	key  string
	role string
}

type zombie struct {
	key   string
	pub   [32]byte
	ip    string
	tries int
}

type keptEntry struct {
	peer    tunnel.Peer
	grants  [2]deadline
	ngrants int
}

type deadline struct{ mono, wall int64 }

type keptSnapshot struct{ entries []keptEntry }

// Controller owns the circle peers of one sidecar. All methods are safe for
// concurrent use. Its lock is held across calls into the tunnel and the filter,
// which is why nothing the tunnel calls back into may take it: SetPeers asks for
// the kept peers through Kept, which reads an immutable snapshot.
type Controller struct {
	cfg Config

	mu      sync.Mutex
	byKey   map[string]*peer
	byAddr  map[netip.Addr]*peer
	links   map[string]linkRef
	zombies map[string]*zombie // peers whose removal from the device failed, by key

	kept atomic.Pointer[keptSnapshot]
	wake chan struct{}
}

// New returns a Controller. cfg.Tunnel, Filter, Signal, Household and Self are
// required; a Controller built without a Filter refuses every add as
// unsupported, because without the packet filter a circle peer would be able to
// reach the whole mesh interface.
func New(cfg Config) *Controller {
	if cfg.Now == nil {
		start := time.Now()
		cfg.Now = func() (int64, int64) {
			t := time.Now()
			return int64(t.Sub(start)), t.UnixNano()
		}
	}
	if cfg.After == nil {
		cfg.After = time.After
	}
	c := &Controller{
		cfg:     cfg,
		byKey:   make(map[string]*peer),
		byAddr:  make(map[netip.Addr]*peer),
		links:   make(map[string]linkRef),
		zombies: make(map[string]*zombie),
		wake:    make(chan struct{}, 1),
	}
	c.kept.Store(&keptSnapshot{})
	return c
}

// ---------------------------------------------------------------- messages

// OnCirclePeerAdd handles circle_peer_add and answers with an ack.
func (c *Controller) OnCirclePeerAdd(m signal.CirclePeerAdd) {
	if !signal.ValidLinkID(m.LinkID) {
		slog.Warn("circle link: add without a usable link_id, not answering")
		return
	}
	c.mu.Lock()
	rej := c.addLocked(m)
	c.mu.Unlock()
	if rej != nil {
		slog.Warn("circle link: add refused", "link_id", m.LinkID, "reason", rej.reason, "why", rej.why)
		c.cfg.Signal.SendCirclePeerAck(signal.NewCirclePeerAck(m.LinkID, signal.CircleOpAdd, signal.CircleStateRejected, rej.reason))
		return
	}
	c.cfg.Signal.SendCirclePeerAck(signal.NewCirclePeerAck(m.LinkID, signal.CircleOpAdd, signal.CircleStateReady, ""))
	select {
	case c.wake <- struct{}{}:
	default:
	}
}

// OnCirclePeerExtend handles circle_peer_extend and answers with an ack.
func (c *Controller) OnCirclePeerExtend(m signal.CirclePeerExtend) {
	if !signal.ValidLinkID(m.LinkID) {
		slog.Warn("circle link: extend without a usable link_id, not answering")
		return
	}
	c.mu.Lock()
	rej := c.extendLocked(m)
	c.mu.Unlock()
	if rej != nil {
		slog.Warn("circle link: extend refused", "link_id", m.LinkID, "reason", rej.reason, "why", rej.why)
		c.cfg.Signal.SendCirclePeerAck(signal.NewCirclePeerAck(m.LinkID, signal.CircleOpExtend, signal.CircleStateRejected, rej.reason))
		return
	}
	c.cfg.Signal.SendCirclePeerAck(signal.NewCirclePeerAck(m.LinkID, signal.CircleOpExtend, signal.CircleStateReady, ""))
}

// OnCirclePeerRemove handles circle_peer_remove. It has no ack. A link id this
// sidecar does not currently hold, or no longer owns the side it once held, is
// ignored: that is the late remove after a supersede, and the replay of a
// removal that was already applied.
func (c *Controller) OnCirclePeerRemove(m signal.CirclePeerRemove) {
	if !signal.ValidLinkID(m.LinkID) {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sweepExpiredLocked()
	ref, ok := c.links[m.LinkID]
	if !ok {
		slog.Debug("circle link: remove for a link not held, ignored", "link_id", m.LinkID)
		return
	}
	p := c.byKey[ref.key]
	if p == nil {
		delete(c.links, m.LinkID)
		return
	}
	slog.Info("circle link: removed by the coordinator", "link_id", m.LinkID, "reason", safeReason(m.Reason), "mesh_ip", p.addr)
	c.dropGrantLocked(p, ref.role)
}

// safeReason keeps a hostile reason string out of the log.
func safeReason(r string) string {
	switch r {
	case signal.CircleRemoveEnded, signal.CircleRemoveRevoked, signal.CircleRemoveExpired, signal.CircleRemoveSuperseded:
		return r
	}
	return "unknown"
}

// ---------------------------------------------------------------- add

func (c *Controller) addLocked(m signal.CirclePeerAdd) *rejection {
	if c.cfg.Filter == nil || c.cfg.Tunnel == nil {
		return reject(signal.CircleReasonUnsupported, "no packet filter")
	}
	c.sweepExpiredLocked()

	self, selfKey := c.cfg.Self()
	if !self.IsValid() {
		return reject(signal.CircleReasonNotEnrolled, "no mesh address yet")
	}
	r, rej := validateShape(m)
	if rej != nil {
		return rej
	}
	if rej := c.judgeAddrLocked(r, self, selfKey); rej != nil {
		return rej
	}

	// A link id names one side of one peer. The same id turning up for a
	// different peer or the other side is a coordinator bug.
	if ref, ok := c.links[r.linkID]; ok && (ref.key != r.key || ref.role != r.role) {
		return reject(signal.CircleReasonInvalidPeer, "link id already in use for another peer")
	}

	// A removal that failed earlier is retried now; if the old peer is still on
	// the device, adding the same key or address would stack on it.
	if z := c.zombieFor(r.key, r.addr); z != nil && !c.retryZombieLocked(z) {
		return reject(signal.CircleReasonTunnelError, "an earlier removal of this peer has not completed")
	}

	if p := c.byKey[r.key]; p != nil {
		return c.updateExistingLocked(p, r)
	}
	if len(c.byKey) >= MaxPeers {
		return reject(signal.CircleReasonLimit, "too many circle peers")
	}
	return c.installFreshLocked(r)
}

// judgeAddrLocked applies the address rules that depend on this host and on who
// else holds the address or the key.
func (c *Controller) judgeAddrLocked(r request, self netip.Addr, selfKey string) *rejection {
	switch {
	case r.addr == self:
		return reject(signal.CircleReasonAddressConflict, "peer address is this node's own")
	case selfKey != "" && r.key == selfKey:
		return reject(signal.CircleReasonInvalidPeer, "peer key is this node's own")
	}
	// The roster's address rules, so one definition of a usable mesh address
	// covers household and circle peers.
	if _, err := c.cfg.Household.CheckMeshAddr(r.addr.String(), self); err != nil {
		return reject(signal.CircleReasonInvalidPeer, err.Error())
	}
	// Another household owns a different /24 of the pool. An address in this
	// host's own /24 that no household peer holds yet is where a household
	// machine will appear, not where a stranger lives.
	if own, err := self.Prefix(24); err == nil && own.Contains(r.addr) {
		return reject(signal.CircleReasonAddressConflict, "peer address is inside this household's own range")
	}
	if c.cfg.Household.IsHousehold(r.addr.As4()) {
		return reject(signal.CircleReasonAddressConflict, "peer address belongs to a household machine")
	}
	// The same WireGuard key names one device peer. Adding a household machine's
	// key as a circle peer would rewrite that machine's allowed-ips.
	if c.cfg.Household.HasKey(r.key) {
		return reject(signal.CircleReasonAddressConflict, "peer key belongs to a household machine")
	}
	if other := c.byAddr[r.addr]; other != nil && other.key != r.key {
		return reject(signal.CircleReasonAddressConflict, "address already held by another circle key")
	}
	if p := c.byKey[r.key]; p != nil && p.addr != r.addr {
		return reject(signal.CircleReasonAddressConflict, "key already held at another address")
	}
	return nil
}

// updateExistingLocked applies an add for a key this sidecar already holds. The
// add is a new link on the same peer, a replay of a link already held, or the
// other side of the peer. Only the grant changes: the device peer, the route and
// the transport are left as they are.
func (c *Controller) updateExistingLocked(p *peer, r request) *rejection {
	mono, wall := c.cfg.Now()
	side := p.grantFor(r.role)
	old := *side
	next := &grant{
		linkID:       r.linkID,
		port:         r.port,
		tokenSHA:     r.token,
		monoDeadline: mono + int64(r.ttl),
		wallDeadline: wall + int64(r.ttl),
	}
	*side = next
	if err := c.setEntryLocked(p, mono, wall); err != nil {
		*side = old
		return reject(signal.CircleReasonTunnelError, "packet filter refused the entry: "+err.Error())
	}
	if old != nil && old.linkID != next.linkID {
		delete(c.links, old.linkID) // superseded: a late remove for the old id is now a no-op
	}
	c.links[next.linkID] = linkRef{key: p.key, role: r.role}
	c.publishLocked()
	slog.Info("circle link: grant updated", "link_id", r.linkID, "role", r.role, "mesh_ip", p.addr, "ttl", r.ttl)
	return nil
}

// installFreshLocked adds a peer this sidecar does not hold. The order is the
// filter entry, the WireGuard peer, the host route; a failure at any step undoes
// the earlier ones and nothing is left behind.
func (c *Controller) installFreshLocked(r request) *rejection {
	mono, wall := c.cfg.Now()
	pub, ok := decodeKey(r.key)
	if !ok {
		return reject(signal.CircleReasonInvalidPeer, "peer key does not decode")
	}
	p := &peer{key: r.key, pub: pub, addr: r.addr, addedWall: wall}
	*p.grantFor(r.role) = &grant{
		linkID:       r.linkID,
		port:         r.port,
		tokenSHA:     r.token,
		monoDeadline: mono + int64(r.ttl),
		wallDeadline: wall + int64(r.ttl),
	}
	c.planTransport(p, r, mono)

	// 1. The filter entry first, so no packet of this peer is ever judged
	// without one.
	if err := c.cfg.Filter.SetEntry(p.addr, p.filterGrant(mono, wall)); err != nil {
		return reject(signal.CircleReasonTunnelError, "packet filter refused the entry: "+err.Error())
	}
	// The kept snapshot is updated before the device is touched. A SetPeers that
	// lands in between then includes this peer instead of removing it.
	c.byKey[p.key], c.byAddr[p.addr] = p, p
	c.links[r.linkID] = linkRef{key: p.key, role: r.role}
	c.publishLocked()

	// 2. The WireGuard peer, then its relay flag.
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		c.rollbackLocked(p)
		return reject(signal.CircleReasonTunnelError, "wireguard refused the peer")
	}
	c.markRelayLocked(p)

	// 3. The host route.
	if err := c.cfg.Tunnel.AddPeerRoute(p.addr.String()); err != nil {
		c.rollbackLocked(p)
		return reject(signal.CircleReasonTunnelError, "could not install the host route")
	}
	p.routeAdded = true

	slog.Info("circle link: peer added", "link_id", r.linkID, "role", r.role, "mesh_ip", p.addr,
		"port", r.port, "ttl", r.ttl, "transport", p.tp.String(), "direct_window", r.window)
	return nil
}

// planTransport decides how the peer's first packets go.
//
//   - A direct window of 0, or no usable endpoint, means no direct try: the relay
//     if there is one, else nothing is sent.
//   - Otherwise the first choice is a LAN candidate the message supplied that
//     is on this host's own subnet, else the public endpoint. The relay takes
//     over after the window (see sweepLocked) when there is one.
//   - The household LAN-direct and punch-trial machinery is not involved. It
//     reads the household store and arms 12 to 15 second trials with the relay
//     off, which would stall an interactive request.
func (c *Controller) planTransport(p *peer, r request, mono int64) {
	relay := c.cfg.Relay != nil
	endpoint := ""
	if r.window > 0 && !(relay && c.cfg.SkipDirect != nil && c.cfg.SkipDirect()) {
		if c.cfg.PickLAN != nil && len(r.lans) > 0 {
			if ep, ok := c.cfg.PickLAN(r.lans); ok {
				endpoint = ep
			}
		}
		if endpoint == "" {
			endpoint = r.wan
		}
	}
	switch {
	case endpoint != "":
		p.tp, p.endpoint, p.keepalive = tpDirect, endpoint, probeKeepalive
		p.windowEnd = mono + int64(r.window)
	case relay:
		p.tp, p.keepalive = tpRelay, steadyKeepalive
	default:
		// Nothing to send to and nowhere to send it. A keepalive would only
		// make WireGuard log "no known endpoint" every few seconds.
		p.tp, p.keepalive = tpNone, 0
	}
	// A machine that only lends lets the requester speak first (see lenderKickAfter).
	if p.tp != tpNone {
		p.quietStart(mono)
	}
	// One poke, 1.5 seconds in, unless the direct window ends before it would run.
	if p.tp != tpNone && (p.windowEnd == 0 || r.window > pokeAfter) {
		p.pokeAt = mono + int64(pokeAfter)
	}
}

// markRelayLocked sets the relay routing flag to match the peer's transport.
func (c *Controller) markRelayLocked(p *peer) {
	if c.cfg.Relay == nil {
		return
	}
	switch p.tp {
	case tpRelay:
		c.cfg.Relay.SetPeerRouting(p.pub, true)
		p.relayMarked = true
	case tpDirect:
		// Off, explicitly: a direct try must not be pulled onto the relay by a
		// stale flag from an earlier life of the same key.
		c.cfg.Relay.SetPeerRouting(p.pub, false)
		p.relayMarked = false
	}
}

// rollbackLocked undoes a failed install, newest step first.
func (c *Controller) rollbackLocked(p *peer) {
	c.unpublishLocked(p)
	if p.routeAdded {
		_ = c.cfg.Tunnel.RemovePeerRoute(p.addr.String())
	}
	// The device may hold a half-applied peer when AddPeer failed, so remove it
	// whenever the add was attempted.
	if err := c.cfg.Tunnel.RemovePeer(p.key); err != nil {
		slog.Error("circle link: rollback could not remove the wireguard peer", "pubkey_prefix", util.TruncatePubkey(p.key), "err", errText(err, p.key, p.endpoint))
		c.addZombieLocked(p)
	}
	if c.cfg.Relay != nil && p.relayMarked {
		c.cfg.Relay.SetPeerRouting(p.pub, false)
	}
	c.cfg.Filter.RemoveEntry(p.addr)
}

// ---------------------------------------------------------------- extend

func (c *Controller) extendLocked(m signal.CirclePeerExtend) *rejection {
	c.sweepExpiredLocked()
	ref, ok := c.links[m.LinkID]
	if !ok {
		return reject(signal.CircleReasonUnknownGrant, "link not held")
	}
	p := c.byKey[ref.key]
	if p == nil {
		return reject(signal.CircleReasonUnknownGrant, "link not held")
	}
	if m.TTLSeconds < 1 {
		return reject(signal.CircleReasonInvalidPeer, "ttl_s must be positive")
	}
	mono, wall := c.cfg.Now()
	ttl := clampTTL(m.TTLSeconds)
	side := p.grantFor(ref.role)
	g := *side
	prevMono, prevWall := g.monoDeadline, g.wallDeadline
	g.monoDeadline, g.wallDeadline = mono+int64(ttl), wall+int64(ttl)
	if err := c.setEntryLocked(p, mono, wall); err != nil {
		g.monoDeadline, g.wallDeadline = prevMono, prevWall
		return reject(signal.CircleReasonTunnelError, "packet filter refused the entry: "+err.Error())
	}
	c.publishLocked()

	// A direct path follows the other machine's current endpoint. The relay path
	// has no endpoint to follow, and a peer that fell back to the relay is not
	// sent back to a direct try by an extend: it has no direct window to run.
	if p.tp == tpDirect && m.PeerEndpoint != nil {
		if ep, ok := publicEndpoint(*m.PeerEndpoint); ok && ep != p.endpoint {
			// AddPeer on a peer the device holds updates it in place, keeping its
			// session. UpdatePeerEndpoint would log the endpoint at info level.
			prev := p.endpoint
			p.endpoint = ep
			if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
				p.endpoint = prev
				slog.Warn("circle link: could not follow the peer's new endpoint", "link_id", m.LinkID, "err", errText(err, p.key, p.endpoint, ep))
			} else {
				c.publishLocked()
			}
		}
	}
	slog.Info("circle link: extended", "link_id", m.LinkID, "mesh_ip", p.addr, "ttl", ttl)
	return nil
}

// ---------------------------------------------------------------- filter

// filterGrant is the single filter entry for the peer: the port of each live
// side, and one deadline. The filter keeps one deadline per peer, so when both
// sides are held it is the later one; the controller removes the earlier side
// itself when its time comes (sweepExpiredLocked).
func (p *peer) filterGrant(mono, wall int64) tunnel.Grant {
	var g tunnel.Grant
	var longest time.Duration
	if in := p.inbound; in != nil && !in.expired(mono, wall) {
		g.InPort = in.port
		if r := in.remaining(mono, wall); r > longest {
			longest = r
		}
	}
	if out := p.outbound; out != nil && !out.expired(mono, wall) {
		g.OutPort = out.port
		if r := out.remaining(mono, wall); r > longest {
			longest = r
		}
	}
	g.TTL = longest
	return g
}

func (c *Controller) setEntryLocked(p *peer, mono, wall int64) error {
	return c.cfg.Filter.SetEntry(p.addr, p.filterGrant(mono, wall))
}

// ---------------------------------------------------------------- remove

// dropGrantLocked removes one side of a peer. When no side is left the whole
// peer goes.
func (c *Controller) dropGrantLocked(p *peer, role string) {
	side := p.grantFor(role)
	if g := *side; g != nil {
		delete(c.links, g.linkID)
	}
	*side = nil
	if p.inbound == nil && p.outbound == nil {
		c.teardownLocked(p)
		return
	}
	mono, wall := c.cfg.Now()
	if err := c.setEntryLocked(p, mono, wall); err != nil {
		slog.Error("circle link: could not narrow the filter entry after one side ended", "mesh_ip", p.addr, "err", err)
	}
	c.publishLocked()
}

// teardownLocked removes a peer completely. The order is the kept snapshot, the
// host route, the WireGuard peer, the relay flag, and the filter entry last: the
// entry keeps confining the peer for as long as the peer can still send. If the
// device refuses the removal the peer is retried (see sweepLocked) and, in the
// meantime, the filter has no entry for its address and drops everything.
func (c *Controller) teardownLocked(p *peer) {
	c.unpublishLocked(p)
	if err := c.cfg.Tunnel.RemovePeerRoute(p.addr.String()); err != nil {
		slog.Warn("circle link: could not remove the host route", "mesh_ip", p.addr, "err", err)
	}
	if err := c.cfg.Tunnel.RemovePeer(p.key); err != nil {
		slog.Error("circle link: could not remove the wireguard peer, will retry", "pubkey_prefix", util.TruncatePubkey(p.key), "err", errText(err, p.key, p.endpoint))
		c.addZombieLocked(p)
	}
	if c.cfg.Relay != nil {
		c.cfg.Relay.SetPeerRouting(p.pub, false)
	}
	c.cfg.Filter.RemoveEntry(p.addr)
	slog.Info("circle link: peer removed", "mesh_ip", p.addr)
}

// unpublishLocked takes the peer out of every table and out of the snapshot
// SetPeers reads.
func (c *Controller) unpublishLocked(p *peer) {
	for _, g := range p.grants() {
		delete(c.links, g.linkID)
	}
	delete(c.byKey, p.key)
	if c.byAddr[p.addr] == p {
		delete(c.byAddr, p.addr)
	}
	c.publishLocked()
}

func (c *Controller) addZombieLocked(p *peer) {
	c.zombies[p.key] = &zombie{key: p.key, pub: p.pub, ip: p.addr.String()}
}

func (c *Controller) zombieFor(key string, addr netip.Addr) *zombie {
	if z := c.zombies[key]; z != nil {
		return z
	}
	for _, z := range c.zombies {
		if z.ip == addr.String() {
			return z
		}
	}
	return nil
}

func (c *Controller) retryZombieLocked(z *zombie) bool {
	_ = c.cfg.Tunnel.RemovePeerRoute(z.ip)
	if err := c.cfg.Tunnel.RemovePeer(z.key); err != nil {
		z.tries++
		return false
	}
	delete(c.zombies, z.key)
	return true
}

// ---------------------------------------------------------------- household

// HouseholdChanged tells the controller that these roster entries are now
// household machines, and, when self is not empty, that this node's own address
// is self. A circle peer whose address or key one of them claims is removed, and
// so is one that now sits on self or in self's /24.
//
// The alternative, keeping the circle peer and refusing the household entry,
// would take one of the owner's own machines offline for the sake of a link that
// lasts at most thirty minutes and that the requester can redo over the relay.
// The household roster is the coordinator's record of the owner's machines;
// the circle peer is a short lease. It also matters mechanically: while the
// circle peer's filter entry exists it wins over the household record, and
// wireguard-go hands an allowed-ip to whichever peer was configured last, so
// leaving both in place would restrict a household machine to one port and
// misroute its packets. Callers invoke this after the household store has taken
// the entries and before the tunnel is configured for them, so the circle peer's
// route and device peer are gone before the household's are installed.
func (c *Controller) HouseholdChanged(self string, entries []signal.RosterPeer) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var own netip.Prefix
	var selfAddr netip.Addr
	if a, err := netip.ParseAddr(self); err == nil && a.Is4() {
		selfAddr = a
		own, _ = a.Prefix(24)
	}
	var doomed []*peer
	seen := make(map[*peer]bool)
	mark := func(p *peer) {
		if p != nil && !seen[p] {
			seen[p] = true
			doomed = append(doomed, p)
		}
	}
	for _, e := range entries {
		if a, err := netip.ParseAddr(e.MeshIP); err == nil {
			mark(c.byAddr[a])
		}
		mark(c.byKey[e.WGPublicKey])
	}
	if selfAddr.IsValid() {
		for _, p := range c.byKey {
			if p.addr == selfAddr || own.Contains(p.addr) {
				mark(p)
			}
		}
	}
	for _, p := range doomed {
		var ids []string
		for _, g := range p.grants() {
			ids = append(ids, g.linkID)
		}
		slog.Warn("circle link: removed because a household machine now holds its address",
			"link_ids", strings.Join(ids, ","), "mesh_ip", p.addr)
		c.teardownLocked(p)
	}
}

// Clear removes every circle peer. POST /unpair calls it: the node is leaving
// its household and must not keep a stranger's peer.
func (c *Controller) Clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := len(c.byKey)
	for _, p := range sortedPeers(c.byKey) {
		c.teardownLocked(p)
	}
	for _, z := range c.zombies {
		c.retryZombieLocked(z)
	}
	if n > 0 {
		slog.Info("circle link: all circle peers cleared", "count", n)
	}
}

func sortedPeers(m map[string]*peer) []*peer {
	out := make([]*peer, 0, len(m))
	for _, p := range m {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].addr.Less(out[j].addr) })
	return out
}

// ---------------------------------------------------------------- the snapshot SetPeers keeps

// publishLocked rebuilds the snapshot Kept reads.
func (c *Controller) publishLocked() {
	snap := &keptSnapshot{}
	for _, p := range sortedPeers(c.byKey) {
		e := keptEntry{peer: p.tunnelPeer()}
		for _, g := range p.grants() {
			e.grants[e.ngrants] = deadline{g.monoDeadline, g.wallDeadline}
			e.ngrants++
		}
		if e.ngrants > 0 {
			snap.entries = append(snap.entries, e)
		}
	}
	c.kept.Store(snap)
}

// Kept returns the peers a SetPeers must carry over: every live circle peer, in
// the configuration it has now. A peer past its deadline on either clock is left
// out, so a welcome can neither drop a live peer nor bring back an expired one.
// It takes no lock and calls nothing, so the tunnel may call it with its own
// lock held.
func (c *Controller) Kept() []tunnel.Peer {
	snap := c.kept.Load()
	if snap == nil || len(snap.entries) == 0 {
		return nil
	}
	mono, wall := c.cfg.Now()
	var out []tunnel.Peer
	for _, e := range snap.entries {
		for i := 0; i < e.ngrants; i++ {
			if mono < e.grants[i].mono && wall < e.grants[i].wall {
				out = append(out, e.peer)
				break
			}
		}
	}
	return out
}

// errText returns err as log text with the peer's key and endpoint cut out. The
// tunnel's errors quote the key in full and sometimes the endpoint, and neither
// belongs in a log: a key only as the truncated prefix, an address of another
// household only at debug level.
func errText(err error, key string, endpoints ...string) string {
	if err == nil {
		return ""
	}
	text := err.Error()
	if key != "" {
		text = strings.ReplaceAll(text, key, util.TruncatePubkey(key))
		if b, derr := base64.StdEncoding.DecodeString(key); derr == nil {
			text = strings.ReplaceAll(text, hex.EncodeToString(b), util.TruncatePubkey(key))
		}
	}
	for _, ep := range endpoints {
		if ep == "" {
			continue
		}
		text = strings.ReplaceAll(text, ep, "<endpoint>")
		if ap, perr := netip.ParseAddrPort(ep); perr == nil {
			text = strings.ReplaceAll(text, ap.Addr().String(), "<address>")
		}
	}
	// Whatever address is left, however it got into the message.
	text = addrInText.ReplaceAllString(text, "<address>")
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' {
			return ' '
		}
		return r
	}, text)
}

// addrInText matches an IPv4 address or a bracketed IPv6 address, with an
// optional port.
var addrInText = regexp.MustCompile(`\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?|\[[0-9A-Fa-f:.]+\](?::\d+)?`)

func decodeKey(k string) ([32]byte, bool) {
	var out [32]byte
	b, err := base64.StdEncoding.DecodeString(k)
	if err != nil || len(b) != 32 {
		return out, false
	}
	copy(out[:], b)
	return out, true
}

// ---------------------------------------------------------------- views

// GrantView is one side of a peer as GET /circle-peers shows it.
type GrantView struct {
	LinkID      string
	Port        uint16
	TokenSHA256 string // inbound only
	ExpiresInS  int
}

// PeerView is one circle peer as GET /circle-peers shows it. PubKey is for the
// caller's reachability query and is never written out.
type PeerView struct {
	MeshIP    string
	PubKey    string
	Transport string // "direct", "derp" or "none"
	Inbound   *GrantView
	Outbound  *GrantView
}

// Peers lists the circle peers, ordered by address. A side past its deadline is
// not listed even if the sweep has not removed it yet, so a gate that trusts
// this list never admits on an expired grant.
func (c *Controller) Peers() []PeerView {
	c.mu.Lock()
	defer c.mu.Unlock()
	mono, wall := c.cfg.Now()
	var out []PeerView
	for _, p := range sortedPeers(c.byKey) {
		v := PeerView{MeshIP: p.addr.String(), PubKey: p.key, Transport: p.tp.String()}
		if g := p.inbound; g != nil && !g.expired(mono, wall) {
			v.Inbound = &GrantView{LinkID: g.linkID, Port: g.port, TokenSHA256: g.tokenSHA, ExpiresInS: secs(g.remaining(mono, wall))}
		}
		if g := p.outbound; g != nil && !g.expired(mono, wall) {
			v.Outbound = &GrantView{LinkID: g.linkID, Port: g.port, ExpiresInS: secs(g.remaining(mono, wall))}
		}
		if v.Inbound != nil || v.Outbound != nil {
			out = append(out, v)
		}
	}
	return out
}

func secs(d time.Duration) int {
	if d < 0 {
		return 0
	}
	return int(d / time.Second)
}

// ---------------------------------------------------------------- the clock

// Run drives the timers until ctx ends: expiry, the direct window, the poke and
// the stale-path check. It sleeps while there is nothing to hold, and wakes when
// a peer is added. Tests call Sweep directly with a fake clock instead.
func (c *Controller) Run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-c.wake:
		}
		for {
			busy, fast := c.status()
			if !busy {
				break
			}
			wait := slowTick
			if fast {
				wait = fastTick
			}
			select {
			case <-ctx.Done():
				return
			case <-c.cfg.After(wait):
			}
			c.Sweep()
		}
	}
}

// status reports whether anything is held and whether a transport timer is due
// soon enough to want the fast tick.
func (c *Controller) status() (busy, fast bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, p := range c.byKey {
		if p.windowEnd != 0 || p.pokeAt != 0 || p.kickAt != 0 {
			fast = true
		}
	}
	return len(c.byKey) > 0 || len(c.zombies) > 0, fast
}

// Sweep runs the timers once. It is what the Run loop calls on each tick.
func (c *Controller) Sweep() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sweepLocked()
}

func (c *Controller) sweepLocked() {
	c.sweepExpiredLocked()
	mono, wall := c.cfg.Now()
	for _, p := range sortedPeers(c.byKey) {
		c.transportTimersLocked(p, mono, wall)
	}
	for _, z := range c.zombies {
		if z.tries >= zombieTries {
			slog.Error("circle link: giving up removing a wireguard peer", "mesh_ip", z.ip)
			delete(c.zombies, z.key)
			continue
		}
		c.retryZombieLocked(z)
	}
}

// sweepExpiredLocked retires every side past its deadline on either clock, and
// every peer left with no side. This is the sidecar's own clock acting, with or
// without a coordinator; the packet filter has already stopped passing the
// peer's packets at the deadline.
func (c *Controller) sweepExpiredLocked() {
	mono, wall := c.cfg.Now()
	for _, p := range sortedPeers(c.byKey) {
		for _, role := range []string{signal.CircleRoleLender, signal.CircleRoleRequester} {
			g := *p.grantFor(role)
			if g != nil && g.expired(mono, wall) {
				slog.Info("circle link: expired on this machine's clock", "link_id", g.linkID, "mesh_ip", p.addr)
				c.dropGrantLocked(p, role)
				if c.byKey[p.key] != p {
					break // the peer itself is gone
				}
			}
		}
	}
}

// transportTimersLocked runs the poke, the direct window and the stale check.
func (c *Controller) transportTimersLocked(p *peer, mono, wall int64) {
	if p.kickAt != 0 && mono >= p.kickAt {
		c.kickLocked(p)
	}
	needHS := p.pokeAt != 0 || p.windowEnd != 0 || (p.confirmed && mono >= p.nextStale)
	if !needHS {
		return
	}
	hs, _, _, err := c.cfg.Tunnel.PeerReachability(p.key)
	if err != nil {
		return
	}
	fresh := !hs.IsZero() && hs.UnixNano() > p.addedWall

	switch {
	case p.windowEnd != 0 && fresh:
		c.confirmDirectLocked(p, mono)
	case p.windowEnd != 0 && mono >= p.windowEnd && c.cfg.Relay != nil:
		c.flipToRelayLocked(p, mono, "no handshake inside the direct window")
	case p.windowEnd != 0 && mono >= p.windowEnd:
		// No relay to fall back to: the direct try simply goes on at the
		// standard keepalive, and the peer is reachable if the other side gets
		// through. The engine uses its own fallback.
		c.endWindowLocked(p)
	case p.pokeAt != 0 && mono >= p.pokeAt && fresh:
		p.pokeAt = 0
	case p.pokeAt != 0 && mono >= p.pokeAt:
		c.pokeLocked(p, mono)
	}

	if p.confirmed && p.tp == tpDirect && mono >= p.nextStale {
		p.nextStale = mono + int64(staleCheckEvery)
		// A confirmed direct path is judged by the three minute window, not by a
		// short one: a healthy link re-handshakes only about every two minutes.
		if c.cfg.Relay != nil && (hs.IsZero() || time.Duration(wall-hs.UnixNano()) >= FreshWindow) {
			c.flipToRelayLocked(p, mono, "direct path went stale")
		}
	}
}

// kickLocked turns the keepalive on for a peer that has only been listening. WireGuard
// sends a handshake initiation when a keepalive is switched on and there is no
// session, and a keepalive packet when there is one, so this starts the lender's
// side of the handshake only if the requester's has not already.
func (c *Controller) kickLocked(p *peer) {
	p.kickAt = 0
	p.keepalive = p.kickAlive
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		slog.Warn("circle link: could not start the lender's keepalive", "mesh_ip", p.addr, "err", errText(err, p.key, p.endpoint))
	}
	c.publishLocked()
}

// endWindowLocked closes a direct window that has no relay behind it.
func (c *Controller) endWindowLocked(p *peer) {
	p.windowEnd = 0
	p.kickAt = 0
	p.keepalive = steadyKeepalive
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		slog.Warn("circle link: could not restore the standard keepalive", "mesh_ip", p.addr, "err", errText(err, p.key, p.endpoint))
	}
	c.publishLocked()
}

// confirmDirectLocked records that a handshake arrived during the window. A
// handshake can also arrive over the relay (the other side may already have
// fallen back and roamed this peer onto the relay), so the live endpoint decides
// which transport this is.
func (c *Controller) confirmDirectLocked(p *peer, mono int64) {
	p.windowEnd = 0
	p.pokeAt = 0
	ep, err := c.cfg.Tunnel.PeerEndpoint(p.key)
	if err == nil && derp.IsSyntheticEndpointPrefix(ep) {
		p.tp, p.endpoint, p.keepalive = tpRelay, "", steadyKeepalive
		p.kickAt = 0
		c.markRelayLocked(p)
		c.publishLocked()
		slog.Info("circle link: handshake arrived over the relay", "mesh_ip", p.addr)
		return
	}
	p.confirmed = true
	p.nextStale = mono + int64(staleCheckEvery)
	p.kickAt = 0
	p.keepalive = steadyKeepalive
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		slog.Warn("circle link: could not restore the standard keepalive", "mesh_ip", p.addr, "err", errText(err, p.key, p.endpoint))
	}
	c.publishLocked()
	slog.Info("circle link: direct path confirmed", "mesh_ip", p.addr)
}

// flipToRelayLocked moves the peer onto the relay. The peer is removed and added
// again so that WireGuard starts a handshake over the relay at once: an endpoint
// change alone does not, and the old direct attempts would only retry after the
// next five second timer.
func (c *Controller) flipToRelayLocked(p *peer, mono int64, why string) {
	_, wall := c.cfg.Now()
	direct := p.endpoint // kept only to cut it out of an error message
	p.tp, p.endpoint, p.keepalive = tpRelay, "", steadyKeepalive
	p.windowEnd, p.confirmed, p.nextStale, p.kickAt = 0, false, 0, 0
	p.quietStart(mono)
	p.addedWall = wall
	c.cfg.Relay.SetPeerRouting(p.pub, true)
	p.relayMarked = true
	_ = c.cfg.Tunnel.RemovePeer(p.key)
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		p.stepFails++
		slog.Error("circle link: could not move the peer to the relay", "mesh_ip", p.addr, "err", errText(err, p.key, direct))
		if p.stepFails < maxStepFails {
			p.pokeAt = mono + int64(time.Second) // try again through the poke path
		}
		c.publishLocked()
		return
	}
	p.stepFails = 0
	p.pokeAt = mono + int64(pokeAfter)
	c.publishLocked()
	slog.Info("circle link: peer moved to the relay", "mesh_ip", p.addr, "why", why)
}

// pokeLocked re-adds a peer that has had no handshake 1.5 seconds after it was
// added. The other machine got its add at nearly the same moment, and the first
// initiation can arrive before that side has installed the peer. wireguard-go
// sends at most one initiation per five seconds per peer object, so only a new
// object can try again now; the peer is removed and added again.
func (c *Controller) pokeLocked(p *peer, mono int64) {
	p.pokeAt = 0
	p.quietStart(mono)
	_ = c.cfg.Tunnel.RemovePeer(p.key)
	if err := c.cfg.Tunnel.AddPeer(p.tunnelPeer()); err != nil {
		p.stepFails++
		slog.Error("circle link: could not re-add the peer", "mesh_ip", p.addr, "err", errText(err, p.key, p.endpoint))
		if p.stepFails < maxStepFails {
			p.pokeAt = mono + int64(time.Second)
		}
		return
	}
	p.stepFails = 0
	_, p.addedWall = c.cfg.Now()
	slog.Debug("circle link: peer re-added to start a handshake", "mesh_ip", p.addr)
}
