package tunnel

// PacketFilter sits between wireguard-go and the TUN device and decides, for
// every plain IP packet, whether the other end of the tunnel may send it or
// receive it. It exists because the host firewall trusts the whole mesh
// interface: the packet filter is the one place that sees every packet the same
// way on Linux, macOS and Windows, whatever transport (direct UDP, LAN, DERP)
// carried it.
//
// Three kinds of address matter, always the address of the PEER end of the
// packet (the destination of a packet leaving this host, the source of one
// arriving):
//
//   - A restricted entry (SetEntry): the peer may reach one TCP port on this
//     host and this host may reach one TCP port on the peer, nothing else. An
//     entry wins over household membership, so an address that is in both is
//     restricted. That is the safe way round: a bug elsewhere that puts a
//     restricted address into the household set breaks that one household peer
//     instead of opening this host to a stranger.
//   - A household peer (the household function): unrestricted in both
//     directions. The function is peerstate.Store.IsHousehold, which answers
//     from an immutable snapshot, so the per-packet cost is one atomic load and
//     one map lookup, no lock and no allocation.
//   - Anything else: dropped. WireGuard only carries addresses that were
//     configured as allowed-ips, all of which come from the roster store today,
//     so nothing that reaches this case could have been delivered anyway.
//
// A packet that cannot be classified as IPv4 is passed through untouched:
// version 6, a version nibble that is neither 4 nor 6, or fewer than 20 bytes.
// wireguard-go then applies its own allowed-ips check, which is what decides
// the outcome. Every peer's allowed-ips is a single IPv4 /32 (household peers
// and, in the next package, circle peers), so wireguard-go delivers no IPv6
// packet to or from any peer. If a peer is ever given an IPv6 allowed-ip, this
// filter must learn IPv6 first.
//
// The filter never returns an error for a drop. On read a dropped packet gets
// size 0, which wireguard-go's TUN reader skips. On write it is left out of the
// batch handed to the device. The buffers wireguard-go owns are never resized,
// swapped or copied, and offsets are passed through untouched.

import (
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/netip"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"golang.zx2c4.com/wireguard/tun"
)

const (
	// maxFlowsPerPeer caps the tracked connections of one restricted peer.
	maxFlowsPerPeer = 256
	// flowIdleTimeout is how long a tracked connection survives without a
	// packet in either direction.
	flowIdleTimeout = 5 * time.Minute
	// flowCloseGrace is how long a connection stays tracked after both sides
	// have sent FIN, so the final ACK and FIN retransmissions still get through.
	flowCloseGrace = 15 * time.Second
	// maxFilterEntries bounds the number of restricted peers, a backstop for
	// the caller's own limit.
	maxFilterEntries = 64
	// maxGrantTTL bounds a grant. It also keeps deadline arithmetic far from
	// overflow.
	maxGrantTTL = 24 * time.Hour
	// warnInterval spaces the log line about packets dropped for an unknown
	// mesh address.
	warnInterval = 30 * time.Second
)

// Packet direction, from the point of view of this host.
const (
	dirIn  uint8 = 1 // from the peer to this host (written to the TUN device)
	dirOut uint8 = 2 // from this host to the peer (read from the TUN device)
)

// TCP flag bits.
const (
	tcpFIN = 0x01
	tcpSYN = 0x02
	tcpRST = 0x04
	tcpACK = 0x10
)

// Errors returned by SetEntry.
var (
	ErrBadPeer        = errors.New("tunnel filter: peer must be a usable IPv4 address other than this host")
	ErrBadTTL         = errors.New("tunnel filter: ttl must be positive")
	ErrTooManyEntries = errors.New("tunnel filter: too many restricted peers")
)

// dropReason says why the filter dropped a packet. It indexes the counters.
type dropReason int

const (
	dropNotPeer   dropReason = iota // to or from an address that is neither household nor restricted
	dropNoSelf                      // restricted peer, but this host's mesh address is not known yet
	dropWrongHost                   // restricted peer, but the other address is not this host
	dropMalformed                   // restricted peer, truncated or inconsistent headers
	dropOptions                     // restricted peer, IP options present
	dropProto                       // restricted peer, not TCP
	dropFragment                    // restricted peer, fragment
	dropPort                        // restricted peer, SYN to a port that is not allowed
	dropNoFlow                      // restricted peer, packet of no tracked connection
	dropExpired                     // restricted peer, entry past its deadline
	dropFlowCap                     // restricted peer, too many connections
	dropGone                        // restricted peer, entry removed while the packet was in flight
	dropPanic                       // the filter itself panicked on this packet
	numDropReasons
)

var dropReasonNames = [numDropReasons]string{
	dropNotPeer:   "not_a_peer",
	dropNoSelf:    "no_self_address",
	dropWrongHost: "wrong_host",
	dropMalformed: "malformed",
	dropOptions:   "ip_options",
	dropProto:     "not_tcp",
	dropFragment:  "fragment",
	dropPort:      "port_not_allowed",
	dropNoFlow:    "no_tracked_flow",
	dropExpired:   "entry_expired",
	dropFlowCap:   "flow_cap",
	dropGone:      "entry_removed",
	dropPanic:     "panic",
}

func (r dropReason) String() string { return dropReasonNames[r] }

// Grant is what a restricted peer may do.
type Grant struct {
	// InPort is the TCP port on this host the peer may connect to, 0 for none.
	InPort uint16
	// OutPort is the TCP port on the peer this host may connect to, 0 for none.
	OutPort uint16
	// TTL is how long the grant lasts from the moment SetEntry is called, at
	// most 24 hours. It is enforced on a monotonic and on a wall clock,
	// whichever runs out first.
	TTL time.Duration
}

// EntryInfo describes one restricted peer for listing.
type EntryInfo struct {
	Peer    netip.Addr
	InPort  uint16
	OutPort uint16
	// Remaining is the time left on the grant, the smaller of the two clocks.
	// It is zero or negative once the grant has run out.
	Remaining time.Duration
	Expired   bool
	// Flows is the number of tracked connections.
	Flows int
}

// FilterStats are the drop counters. Packets that pass are not counted: the
// household path stays free of writes.
type FilterStats struct {
	// Dropped counts drops by reason name.
	Dropped map[string]uint64
}

// Total returns the number of dropped packets over all reasons.
func (s FilterStats) Total() uint64 {
	var n uint64
	for _, v := range s.Dropped {
		n += v
	}
	return n
}

// clock returns two readings of "now" in nanoseconds: a monotonic one (which on
// most systems stops while the machine sleeps) and the wall clock. The filter
// treats a grant as over when either reading has passed its deadline.
type clock func() (mono, wall int64)

// PacketFilter is the packet policy. Create it with NewPacketFilter, pass it to
// tunnel.New with WithPacketPolicy, and manage restricted peers with SetEntry
// and RemoveEntry. All methods are safe for concurrent use.
type PacketFilter struct {
	household func(addr [4]byte) bool
	self      atomic.Uint32 // this host's mesh address, 0 when not known
	table     atomic.Pointer[entryTable]
	watch     atomic.Pointer[watchTable] // nil unless some address is watched, see filter_watch.go
	writeMu   sync.Mutex                 // serialises changes to table and watch
	now       clock

	drops     [numDropReasons]atomic.Uint64
	lastWarn  atomic.Int64 // monotonic ns of the last unknown-address warning
	lastPanic atomic.Int64 // monotonic ns of the last panic log line
}

// entryTable is an immutable map of restricted peers, replaced as a whole.
type entryTable struct {
	m map[[4]byte]*peerEntry
}

// peerEntry is one restricted peer. Everything below mu is guarded by it. The
// per-packet path takes mu once; household packets never reach it.
type peerEntry struct {
	addr [4]byte

	mu           sync.Mutex
	inPort       uint16
	outPort      uint16
	monoDeadline int64
	wallDeadline int64
	removed      bool
	flows        map[uint32]flow
}

// flow is one tracked TCP connection, keyed by peerPort<<16 | localPort.
type flow struct {
	expire int64 // monotonic ns after which the connection counts as idle
	origin uint8 // which side sent the SYN
	fin    uint8 // directions that have sent FIN
}

// NewPacketFilter returns a filter. household answers whether an IPv4 address
// belongs to a household peer right now; it runs on every packet and must not
// block or allocate (peerstate.Store.IsHousehold qualifies). A nil household is
// the empty set.
func NewPacketFilter(household func(addr [4]byte) bool) *PacketFilter {
	if household == nil {
		household = func([4]byte) bool { return false }
	}
	start := time.Now()
	f := &PacketFilter{
		household: household,
		now: func() (int64, int64) {
			t := time.Now()
			return int64(t.Sub(start)), t.UnixNano()
		},
	}
	f.table.Store(&entryTable{m: map[[4]byte]*peerEntry{}})
	return f
}

// SetSelf tells the filter this host's own mesh address. Restricted peers may
// only talk to this address, and this host may only reach them from it. Until
// it is called, every packet of a restricted peer is dropped. A nil or non-IPv4
// address clears it.
func (f *PacketFilter) SetSelf(ip net.IP) {
	var v uint32
	if ip4 := ip.To4(); ip4 != nil {
		v = binary.BigEndian.Uint32(ip4)
	}
	f.self.Store(v)
}

func (f *PacketFilter) selfAddr() [4]byte {
	var a [4]byte
	binary.BigEndian.PutUint32(a[:], f.self.Load())
	return a
}

// SetEntry adds a restricted peer, or replaces the entry of one that exists.
// Replacing changes ports and deadline in place: connections that are still
// allowed under the new ports stay tracked, so a renewal does not cut a request
// in flight, and connections opened under a port that no longer applies are
// forgotten.
func (f *PacketFilter) SetEntry(peer netip.Addr, g Grant) error {
	peer = peer.Unmap()
	if !peer.Is4() || peer.IsUnspecified() || peer.IsLoopback() || peer.IsMulticast() || peer.As4() == f.selfAddr() && f.self.Load() != 0 {
		return ErrBadPeer
	}
	if g.TTL <= 0 || g.TTL > maxGrantTTL {
		return ErrBadTTL
	}
	mono, wall := f.now()
	key := peer.As4()

	f.writeMu.Lock()
	defer f.writeMu.Unlock()
	old := f.table.Load()
	if e, ok := old.m[key]; ok {
		e.mu.Lock()
		if mono >= e.monoDeadline || wall >= e.wallDeadline {
			// The old grant had run out. Connections opened under it must not
			// come back to life because a new one arrives.
			clear(e.flows)
		}
		if g.InPort != e.inPort {
			e.forgetOriginLocked(dirIn)
		}
		if g.OutPort != e.outPort {
			e.forgetOriginLocked(dirOut)
		}
		e.inPort, e.outPort = g.InPort, g.OutPort
		e.monoDeadline, e.wallDeadline = mono+int64(g.TTL), wall+int64(g.TTL)
		e.mu.Unlock()
		slog.Info("tunnel filter: restricted peer updated", "mesh_ip", peer, "in_port", g.InPort, "out_port", g.OutPort, "ttl", g.TTL)
		return nil
	}
	if len(old.m) >= maxFilterEntries {
		return ErrTooManyEntries
	}
	e := &peerEntry{
		addr:         key,
		inPort:       g.InPort,
		outPort:      g.OutPort,
		monoDeadline: mono + int64(g.TTL),
		wallDeadline: wall + int64(g.TTL),
		flows:        make(map[uint32]flow),
	}
	next := &entryTable{m: make(map[[4]byte]*peerEntry, len(old.m)+1)}
	for k, v := range old.m {
		next.m[k] = v
	}
	next.m[key] = e
	f.table.Store(next)
	slog.Info("tunnel filter: restricted peer added", "mesh_ip", peer, "in_port", g.InPort, "out_port", g.OutPort, "ttl", g.TTL)
	return nil
}

// RemoveEntry drops a restricted peer and every connection tracked for it. When
// it returns, no packet that starts to be judged afterwards passes on the
// entry's account, and a packet already in flight is refused if it has not yet
// been admitted. An address with no entry is not a restricted peer: it is a
// household peer or it is dropped. RemoveEntry of an unknown address does
// nothing.
func (f *PacketFilter) RemoveEntry(peer netip.Addr) {
	peer = peer.Unmap()
	if !peer.Is4() {
		return
	}
	key := peer.As4()

	f.writeMu.Lock()
	defer f.writeMu.Unlock()
	old := f.table.Load()
	e, ok := old.m[key]
	if !ok {
		return
	}
	next := &entryTable{m: make(map[[4]byte]*peerEntry, len(old.m))}
	for k, v := range old.m {
		if k != key {
			next.m[k] = v
		}
	}
	f.table.Store(next)
	e.mu.Lock()
	e.removed = true
	e.flows = nil
	e.mu.Unlock()
	slog.Info("tunnel filter: restricted peer removed", "mesh_ip", peer)
}

// Entries lists the restricted peers, ordered by address.
func (f *PacketFilter) Entries() []EntryInfo {
	mono, wall := f.now()
	tbl := f.table.Load()
	out := make([]EntryInfo, 0, len(tbl.m))
	for _, e := range tbl.m {
		e.mu.Lock()
		e.sweepLocked(mono)
		rem := time.Duration(e.monoDeadline - mono)
		if w := time.Duration(e.wallDeadline - wall); w < rem {
			rem = w
		}
		out = append(out, EntryInfo{
			Peer:      netip.AddrFrom4(e.addr),
			InPort:    e.inPort,
			OutPort:   e.outPort,
			Remaining: rem,
			Expired:   rem <= 0,
			Flows:     len(e.flows),
		})
		e.mu.Unlock()
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Peer.Less(out[j].Peer) })
	return out
}

// Stats returns the drop counters since the filter was created.
func (f *PacketFilter) Stats() FilterStats {
	s := FilterStats{Dropped: make(map[string]uint64, numDropReasons)}
	for r := dropReason(0); r < numDropReasons; r++ {
		if n := f.drops[r].Load(); n > 0 {
			s.Dropped[r.String()] = n
		}
	}
	return s
}

// Wrap returns d with the filter applied. The result forwards File, MTU, Name,
// Events, Close and BatchSize to d unchanged.
func (f *PacketFilter) Wrap(d tun.Device) tun.Device {
	return &filteredTUN{Device: d, f: f}
}

// ---------------------------------------------------------------- device

// filteredTUN applies a PacketFilter around a tun.Device.
type filteredTUN struct {
	tun.Device
	f *PacketFilter
}

// scratch holds the batch handed to the device when some packets of a write
// were dropped. It is pooled so the drop path does not allocate per call.
var scratch = sync.Pool{New: func() any { s := make([][]byte, 0, 128); return &s }}

// Read gets packets from the host heading into the tunnel. A dropped packet
// gets size 0, which wireguard-go's TUN reader skips. The packets are examined
// even when the device also returned an error (wireguard-go processes them
// too), and the device's result is returned as it came.
func (w *filteredTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	n, err := w.Device.Read(bufs, sizes, offset)
	w.f.filterRead(bufs, sizes, offset, n)
	return n, err
}

func (f *PacketFilter) filterRead(bufs [][]byte, sizes []int, offset, n int) {
	defer func() {
		// verdict recovers its own panics. This covers the loop around it, and
		// drops everything the loop had not reached.
		if r := recover(); r != nil {
			f.drops[dropPanic].Add(1)
			for i := 0; i < n && i < len(sizes); i++ {
				sizes[i] = 0
			}
		}
	}()
	for i := 0; i < n && i < len(bufs) && i < len(sizes); i++ {
		sz := sizes[i]
		if sz < 1 || offset < 0 || offset+sz > len(bufs[i]) {
			continue // nothing to judge; wireguard-go skips or rejects it itself
		}
		if !f.verdict(bufs[i][offset:offset+sz], dirOut) {
			sizes[i] = 0
		}
	}
}

// Write gets packets that came out of the tunnel toward this host. wireguard-go
// has already checked each source address against the sending peer's
// allowed-ips. Dropped packets are left out of the batch. When nothing is
// dropped the caller's slice goes to the device as it is, and the caller's
// slice is never modified in any case. Several peers' receive loops call Write
// at once, so nothing here is shared between calls except the pooled scratch
// slice, which each call takes for itself.
func (w *filteredTUN) Write(bufs [][]byte, offset int) (int, error) {
	if t := w.f.watch.Load(); t != nil {
		t.observe(bufs, offset) // read-only, before the verdict, see filter_watch.go
	}
	kept, pooled := w.f.filterWrite(bufs, offset)
	if len(kept) == 0 {
		w.f.putScratch(pooled)
		return 0, nil // every packet was dropped; the device is not called
	}
	n, err := w.Device.Write(kept, offset)
	w.f.putScratch(pooled)
	return n, err
}

// filterWrite returns the packets of bufs that may pass. It returns bufs itself
// when all of them may. Otherwise it returns a scratch-backed copy of the
// slice headers (not of the packets), and pooled is the handle to give back
// with putScratch once the device has been called.
func (f *PacketFilter) filterWrite(bufs [][]byte, offset int) (kept [][]byte, pooled *[][]byte) {
	defer func() {
		if r := recover(); r != nil {
			f.drops[dropPanic].Add(1)
			kept = nil // fail closed: the whole batch is dropped
			if pooled != nil {
				*pooled = (*pooled)[:cap(*pooled)] // so putScratch clears whatever was appended
			}
		}
	}()
	first := -1
	for i, b := range bufs {
		if !f.admitWrite(b, offset) {
			first = i
			break
		}
	}
	if first < 0 {
		return bufs, nil
	}
	pooled = scratch.Get().(*[][]byte)
	kept = append((*pooled)[:0], bufs[:first]...)
	for _, b := range bufs[first+1:] {
		if f.admitWrite(b, offset) {
			kept = append(kept, b)
		}
	}
	*pooled = kept
	return kept, pooled
}

// putScratch clears and returns a scratch slice, so the pool does not keep
// wireguard-go's buffers alive.
func (f *PacketFilter) putScratch(p *[][]byte) {
	if p == nil {
		return
	}
	clear(*p)
	*p = (*p)[:0]
	scratch.Put(p)
}

func (f *PacketFilter) admitWrite(b []byte, offset int) bool {
	if offset < 0 || len(b) <= offset {
		return true // nothing to classify; the device decides
	}
	return f.verdict(b[offset:], dirIn)
}

// ---------------------------------------------------------------- verdict

// verdict reports whether the packet may pass. A panic while judging it drops
// the packet.
func (f *PacketFilter) verdict(p []byte, dir uint8) (pass bool) {
	defer func() {
		if r := recover(); r != nil {
			n := f.drops[dropPanic].Add(1)
			if f.spaced(&f.lastPanic) {
				slog.Error("tunnel filter: panic while judging a packet, dropping it", "panic", fmt.Sprint(r), "panics_total", n)
			}
			pass = false
		}
	}()
	// Not IPv4 or too short to carry addresses: leave it to wireguard-go.
	if len(p) < 20 || p[0]>>4 != 4 {
		return true
	}
	var peer [4]byte
	if dir == dirIn {
		peer = [4]byte(p[12:16])
	} else {
		peer = [4]byte(p[16:20])
	}
	if e, ok := f.table.Load().m[peer]; ok {
		return f.restricted(e, p, dir)
	}
	if f.household(peer) {
		return true
	}
	f.dropUnknown(peer, dir)
	return false
}

// restricted judges a packet to or from a restricted peer.
func (f *PacketFilter) restricted(e *peerEntry, p []byte, dir uint8) bool {
	selfV := f.self.Load()
	if selfV == 0 {
		return f.drop(dropNoSelf)
	}
	var self [4]byte
	binary.BigEndian.PutUint32(self[:], selfV)
	// The other end of the packet must be this host. Inbound that rejects
	// packets meant for a household machine behind this one; outbound it
	// rejects packets this host is only forwarding.
	if dir == dirIn {
		if [4]byte(p[16:20]) != self {
			return f.drop(dropWrongHost)
		}
	} else if [4]byte(p[12:16]) != self {
		return f.drop(dropWrongHost)
	}

	// IP options are refused outright: no TCP stack needs them, and source
	// routing or a header length that two parsers read differently is how a
	// filter gets walked around.
	if p[0]&0x0f != 5 {
		if p[0]&0x0f < 5 {
			return f.drop(dropMalformed)
		}
		return f.drop(dropOptions)
	}
	// The protocol and fragment checks come before the minimum TCP length so a
	// short UDP, ICMP or fragment packet is counted for what it is.
	total := int(binary.BigEndian.Uint16(p[2:4]))
	if total > len(p) || total < 20 {
		return f.drop(dropMalformed)
	}
	// Any fragment, including the first, is refused: later fragments carry no
	// ports, and reassembly differences are what overlapping-fragment tricks use.
	if binary.BigEndian.Uint16(p[6:8])&0x3fff != 0 {
		return f.drop(dropFragment)
	}
	if p[9] != 6 {
		return f.drop(dropProto)
	}
	if total < 40 {
		return f.drop(dropMalformed)
	}
	tcp := p[20:total]
	if doff := int(tcp[12]>>4) << 2; doff < 20 || doff > len(tcp) {
		return f.drop(dropMalformed)
	}
	sport := binary.BigEndian.Uint16(tcp[0:2])
	dport := binary.BigEndian.Uint16(tcp[2:4])
	mono, wall := f.now()
	if r := e.admit(dir, sport, dport, tcp[13], mono, wall); r != passed {
		return f.drop(r)
	}
	return true
}

// passed is the "no drop" result of admit.
const passed dropReason = -1

// admit applies the entry's grant and connection table to one TCP packet.
func (e *peerEntry) admit(dir uint8, sport, dport uint16, flags uint8, mono, wall int64) dropReason {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.removed {
		return dropGone
	}
	if mono >= e.monoDeadline || wall >= e.wallDeadline {
		return dropExpired
	}
	var key uint32
	var allowed uint16
	if dir == dirIn {
		key, allowed = uint32(sport)<<16|uint32(dport), e.inPort
	} else {
		key, allowed = uint32(dport)<<16|uint32(sport), e.outPort
	}

	// A connection starts with a SYN that has no ACK, RST or FIN, sent to the
	// port this direction is granted. It is the only packet that creates state.
	if flags&(tcpSYN|tcpACK|tcpRST|tcpFIN) == tcpSYN {
		if allowed == 0 || dport != allowed {
			return dropPort
		}
		if cur, ok := e.flows[key]; !ok || mono >= cur.expire {
			if len(e.flows) >= maxFlowsPerPeer {
				e.sweepLocked(mono)
				if len(e.flows) >= maxFlowsPerPeer {
					return dropFlowCap
				}
			}
		}
		e.flows[key] = flow{expire: mono + int64(flowIdleTimeout), origin: dir}
		return passed
	}

	fl, ok := e.flows[key]
	if !ok {
		return dropNoFlow
	}
	if mono >= fl.expire {
		delete(e.flows, key)
		return dropNoFlow
	}
	switch {
	case flags&tcpRST != 0:
		delete(e.flows, key) // the RST itself still goes through
		return passed
	case flags&tcpFIN != 0:
		fl.fin |= dir
		if fl.fin == dirIn|dirOut {
			fl.expire = mono + int64(flowCloseGrace)
		} else {
			fl.expire = mono + int64(flowIdleTimeout)
		}
	case fl.fin != dirIn|dirOut:
		fl.expire = mono + int64(flowIdleTimeout)
	}
	e.flows[key] = fl
	return passed
}

// sweepLocked removes connections that have been idle too long.
func (e *peerEntry) sweepLocked(mono int64) {
	for k, fl := range e.flows {
		if mono >= fl.expire {
			delete(e.flows, k)
		}
	}
}

// forgetOriginLocked removes the connections that side opened.
func (e *peerEntry) forgetOriginLocked(origin uint8) {
	for k, fl := range e.flows {
		if fl.origin == origin {
			delete(e.flows, k)
		}
	}
}

// ---------------------------------------------------------------- drops

func (f *PacketFilter) drop(r dropReason) bool {
	f.drops[r].Add(1)
	return false
}

// dropUnknown drops a packet whose peer end is neither restricted nor a
// household peer. Most of these are multicast chatter wireguard-go would drop
// anyway, so it only logs for an address inside the mesh range, which is what
// a household peer missing from the store (or a stranger) looks like, and at
// most once per warnInterval.
func (f *PacketFilter) dropUnknown(peer [4]byte, dir uint8) {
	f.drops[dropNotPeer].Add(1)
	if peer[0] != 100 || peer[1]&0xc0 != 64 {
		return
	}
	if !f.spaced(&f.lastWarn) {
		return
	}
	way := "from"
	if dir == dirOut {
		way = "to"
	}
	slog.Warn("tunnel filter: dropped packets to or from a mesh address that is not a household peer",
		"direction", way, "mesh_ip", netip.AddrFrom4(peer),
		"dropped_total", f.drops[dropNotPeer].Load(),
	)
}

// spaced reports whether warnInterval has passed since the time stored in last,
// and if so records now there. It lets a log line that a hostile or broken
// packet stream could trigger at line rate come out once per interval.
func (f *PacketFilter) spaced(last *atomic.Int64) bool {
	mono, _ := f.now()
	prev := last.Load()
	if prev != 0 && mono-prev < int64(warnInterval) {
		return false
	}
	return last.CompareAndSwap(prev, mono|1) // |1 keeps it nonzero at mono 0
}
