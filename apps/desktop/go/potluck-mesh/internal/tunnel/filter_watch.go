package tunnel

import (
	"encoding/binary"
	"net/netip"
	"sync/atomic"
)

// Inbound watch: a read-only count of the packets that come out of the tunnel
// from a few chosen mesh addresses.
//
// A direct-path trial needs to know whether a peer's packets reach this machine
// and decrypt. The filter's Write sees every decrypted inbound IP packet before
// the operating system does, so it can answer that without asking anything of the
// peer's operating system (a machine in stealth mode answers neither a reset nor
// an unreachable for a port nothing listens on). The caller asks for the count
// before and after a moment and compares.
//
// The watch is an observation and nothing else. It runs before the verdict, never
// changes it, never keeps a reference to a buffer and never blocks. With no
// address watched the cost per Write call is one atomic pointer load and a nil
// test, not per packet. With addresses watched it is one pass over the batch,
// comparing the four source bytes of each IPv4 packet with the few watched
// addresses and adding one to a counter on a match. It reads no clock, because
// the caller compares counts, not times.

// maxWatchedInbound bounds the number of watched addresses, a backstop for the
// caller's own limit (one per peer in a direct-path trial).
const maxWatchedInbound = 64

// watchTable is an immutable list, replaced as a whole under writeMu, so the
// per-packet path takes no lock. It is small and scanned linearly: with a handful
// of entries that is faster than hashing.
type watchTable struct {
	items []watchItem
}

type watchItem struct {
	addr uint32 // the four address bytes read as a little-endian word, so one compare per packet
	n    *atomic.Uint64
}

// observe adds one to the counter of every watched address that is the source of
// an IPv4 packet in bufs. offset is where the IP packet starts in each buffer, as
// it is for Write.
func (t *watchTable) observe(bufs [][]byte, offset int) {
	if offset < 0 {
		return
	}
	// A batch usually comes from one peer, so matches are tallied on the stack and
	// added to the shared counter once per batch, not once per packet. The first
	// eight watched addresses are tallied; any beyond that (the limit is far above
	// what a trial needs) add straight to their counter.
	var tally [8]uint32
	items := t.items
	for _, b := range bufs {
		if len(b) < offset+20 || b[offset]>>4 != 4 {
			continue
		}
		src := binary.LittleEndian.Uint32(b[offset+12 : offset+16])
		for i := range items {
			if items[i].addr == src {
				if i < len(tally) {
					tally[i]++
				} else {
					items[i].n.Add(1)
				}
				break
			}
		}
	}
	for i := 0; i < len(items) && i < len(tally); i++ {
		if tally[i] != 0 {
			items[i].n.Add(uint64(tally[i]))
		}
	}
}

// WatchInbound starts counting the IPv4 packets that arrive through the tunnel
// with addr as their source. Watching an address that is already watched keeps its
// count. It does not change what the filter lets through. An address that is not
// IPv4, or more than maxWatchedInbound addresses, is ignored.
func (f *PacketFilter) WatchInbound(addr netip.Addr) {
	addr = addr.Unmap()
	if !addr.Is4() {
		return
	}
	a4 := addr.As4()
	key := binary.LittleEndian.Uint32(a4[:])
	f.writeMu.Lock()
	defer f.writeMu.Unlock()
	old := f.watch.Load()
	var items []watchItem
	if old != nil {
		for _, it := range old.items {
			if it.addr == key {
				return
			}
		}
		if len(old.items) >= maxWatchedInbound {
			return
		}
		items = append(items, old.items...)
	}
	items = append(items, watchItem{addr: key, n: new(atomic.Uint64)})
	f.watch.Store(&watchTable{items: items})
}

// UnwatchInbound stops counting addr and forgets its count. When the last address
// is unwatched the per-Write cost goes back to a pointer load.
func (f *PacketFilter) UnwatchInbound(addr netip.Addr) {
	addr = addr.Unmap()
	if !addr.Is4() {
		return
	}
	a4 := addr.As4()
	key := binary.LittleEndian.Uint32(a4[:])
	f.writeMu.Lock()
	defer f.writeMu.Unlock()
	old := f.watch.Load()
	if old == nil {
		return
	}
	var items []watchItem
	for _, it := range old.items {
		if it.addr != key {
			items = append(items, it)
		}
	}
	if len(items) == len(old.items) {
		return
	}
	if len(items) == 0 {
		f.watch.Store(nil)
		return
	}
	f.watch.Store(&watchTable{items: items})
}

// InboundCount returns how many IPv4 packets with addr as their source have come
// out of the tunnel since addr was watched, or 0 if it is not watched. Counts
// only grow, so a caller that wants "any packet since a moment" takes the count at
// that moment and compares later.
func (f *PacketFilter) InboundCount(addr netip.Addr) uint64 {
	addr = addr.Unmap()
	if !addr.Is4() {
		return 0
	}
	t := f.watch.Load()
	if t == nil {
		return 0
	}
	a4 := addr.As4()
	key := binary.LittleEndian.Uint32(a4[:])
	for _, it := range t.items {
		if it.addr == key {
			return it.n.Load()
		}
	}
	return 0
}
