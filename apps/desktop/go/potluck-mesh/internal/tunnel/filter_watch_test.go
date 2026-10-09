package tunnel

import (
	"bytes"
	"net/netip"
	"sync"
	"testing"
	"time"
)

func batchFrom(srcs ...[4]byte) ([][]byte, []pkt) {
	var pk []pkt
	for i, s := range srcs {
		pk = append(pk, pkt{src: s, dst: selfA, sport: uint16(40000 + i), dport: 8321, flags: fPSHACK, payload: 8})
	}
	return writeBatch(pk...), pk
}

func TestWatchInbound_CountsOnlyWatchedSources(t *testing.T) {
	for _, prof := range allProfiles {
		t.Run(prof.name, func(t *testing.T) {
			w, _, f, _ := wrapped(t, prof, hhA, hhB)
			f.WatchInbound(addr(hhA))
			for _, src := range [][4]byte{hhA, hhB, hhA, hhB, hhB, hhA} {
				// one packet per call: darwin and windows devices take one at a time
				bufs, _ := batchFrom(src)
				if _, err := w.Write(bufs, wgOffset); err != nil {
					t.Fatal(err)
				}
			}
			if got := f.InboundCount(addr(hhA)); got != 3 {
				t.Errorf("count for the watched source = %d, want 3", got)
			}
			if got := f.InboundCount(addr(hhB)); got != 0 {
				t.Errorf("count for a source nobody watches = %d, want 0", got)
			}
			if got := f.InboundCount(addr(circA)); got != 0 {
				t.Errorf("count for an unknown address = %d, want 0", got)
			}
		})
	}
}

// The watch is an observation: the same batch gives the same verdicts and the same bytes at the
// device with and without it, including packets that are dropped, and a watched address whose
// packets are dropped is counted and still dropped.
func TestWatchInbound_ChangesNoVerdict(t *testing.T) {
	for _, prof := range allProfiles {
		t.Run(prof.name, func(t *testing.T) {
			run := func(watch bool) (written [][]byte, stats FilterStats, count uint64) {
				w, d, f, _ := wrapped(t, prof, hhA)
				mustEntry(t, f, circA, 8340, 0, time.Hour)
				if watch {
					f.WatchInbound(addr(hhA))
					f.WatchInbound(addr(circA))
					f.WatchInbound(addr(strngr))
				}
				var pk []pkt
				for i := 0; i < prof.batch; i++ {
					switch i % 4 {
					case 0:
						pk = append(pk, pkt{src: hhA, dst: selfA, sport: uint16(i), dport: 22, flags: fSYN})
					case 1:
						pk = append(pk, pkt{src: circA, dst: selfA, sport: uint16(40000 + i), dport: 8340, flags: fSYN})
					case 2:
						pk = append(pk, pkt{src: circA, dst: selfA, sport: uint16(40000 + i), dport: 22, flags: fSYN}) // dropped
					default:
						pk = append(pk, pkt{src: strngr, dst: selfA, sport: uint16(i), dport: 8340, flags: fSYN}) // dropped
					}
				}
				if _, err := w.Write(writeBatch(pk...), wgOffset); err != nil {
					t.Fatal(err)
				}
				return d.written, f.Stats(), f.InboundCount(addr(strngr)) + f.InboundCount(addr(circA))
			}
			wa, sa, _ := run(false)
			wb, sb, cnt := run(true)
			if len(wa) != len(wb) {
				t.Fatalf("device saw %d packets without the watch and %d with it", len(wa), len(wb))
			}
			for i := range wa {
				if !bytes.Equal(wa[i], wb[i]) {
					t.Errorf("packet %d reached the device differently with the watch", i)
				}
			}
			if sa.Total() != sb.Total() {
				t.Errorf("drops without the watch %d, with it %d", sa.Total(), sb.Total())
			}
			for k, v := range sa.Dropped {
				if sb.Dropped[k] != v {
					t.Errorf("drop reason %q: %d without the watch, %d with it", k, v, sb.Dropped[k])
				}
			}
			if prof.batch >= 4 && cnt == 0 {
				t.Error("packets from watched addresses that the filter dropped were not counted")
			}
		})
	}
}

func TestWatchInbound_WatchKeepsCountUnwatchForgetsIt(t *testing.T) {
	w, _, f, _ := wrapped(t, profLinux, hhA)
	f.WatchInbound(addr(hhA))
	bufs, _ := batchFrom(hhA, hhA)
	w.Write(bufs, wgOffset)
	f.WatchInbound(addr(hhA)) // again: the count stays
	if got := f.InboundCount(addr(hhA)); got != 2 {
		t.Fatalf("count after watching twice = %d, want 2", got)
	}
	w.Write(bufs, wgOffset)
	if got := f.InboundCount(addr(hhA)); got != 4 {
		t.Fatalf("count = %d, want 4", got)
	}
	f.UnwatchInbound(addr(hhA))
	if got := f.InboundCount(addr(hhA)); got != 0 {
		t.Errorf("count after unwatch = %d, want 0", got)
	}
	if f.watch.Load() != nil {
		t.Error("with nothing watched the table must be nil, so Write costs one pointer load")
	}
	w.Write(bufs, wgOffset)
	f.WatchInbound(addr(hhA))
	if got := f.InboundCount(addr(hhA)); got != 0 {
		t.Errorf("a new watch must start at 0, got %d", got)
	}
	f.UnwatchInbound(addr(hhB)) // not watched: nothing happens
	f.UnwatchInbound(netip.MustParseAddr("::1"))
	f.WatchInbound(netip.MustParseAddr("::1"))
	if f.InboundCount(netip.MustParseAddr("::1")) != 0 {
		t.Error("an IPv6 address must be ignored")
	}
}

func TestWatchInbound_BadInputIsIgnored(t *testing.T) {
	_, _, f, _ := wrapped(t, profLinux, hhA)
	f.WatchInbound(addr(hhA))
	tbl := f.watch.Load()
	short := make([]byte, wgOffset+10)
	v6 := make([]byte, wgOffset+40)
	v6[wgOffset] = 0x60
	ok := pkt{src: hhA, dst: selfA, sport: 1, dport: 2, flags: fACK}.bytes()
	okBuf := append(make([]byte, wgOffset), ok...)
	nothing := make([]byte, 0)
	tbl.observe([][]byte{short, v6, nil, nothing, okBuf[:wgOffset+19]}, wgOffset)
	if got := f.InboundCount(addr(hhA)); got != 0 {
		t.Errorf("short, IPv6 and empty buffers were counted: %d", got)
	}
	tbl.observe([][]byte{okBuf}, -1)
	if got := f.InboundCount(addr(hhA)); got != 0 {
		t.Errorf("a negative offset was counted: %d", got)
	}
	tbl.observe([][]byte{okBuf}, wgOffset)
	if got := f.InboundCount(addr(hhA)); got != 1 {
		t.Errorf("a plain packet from the watched source was not counted: %d", got)
	}
}

func TestWatchInbound_OnlyWhatComesOutOfTheTunnel(t *testing.T) {
	w, d, f, _ := wrapped(t, profLinux, hhA)
	f.WatchInbound(addr(hhA))
	// A packet this host sends to hhA is not an inbound packet from it.
	d.readTemplate = [][]byte{pkt{src: selfA, dst: hhA, sport: 8321, dport: 40000, flags: fPSHACK}.bytes()}
	rb, rs := readBufs(1)
	w.Read(rb, rs, wgOffset)
	if got := f.InboundCount(addr(hhA)); got != 0 {
		t.Errorf("a packet read from the host counted as inbound: %d", got)
	}
}

func TestWatchInbound_LimitAndConcurrency(t *testing.T) {
	_, _, f, _ := wrapped(t, profLinux, hhA)
	for i := 0; i < maxWatchedInbound+5; i++ {
		f.WatchInbound(addr([4]byte{100, 64, 5, byte(i)}))
	}
	if n := len(f.watch.Load().items); n != maxWatchedInbound {
		t.Errorf("%d addresses watched, want the limit %d", n, maxWatchedInbound)
	}

	w, _, f2, _ := wrapped(t, profLinux, hhA, hhB)
	bufs, _ := batchFrom(hhA, hhB, hhA)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					w.Write(bufs, wgOffset)
				}
			}
		}()
	}
	for i := 0; i < 2000; i++ {
		f2.WatchInbound(addr(hhA))
		_ = f2.InboundCount(addr(hhA))
		f2.WatchInbound(addr(hhB))
		f2.UnwatchInbound(addr(hhA))
		_ = f2.InboundCount(addr(hhB))
		f2.UnwatchInbound(addr(hhB))
	}
	close(stop)
	wg.Wait()
}

// More than eight watched addresses: the first eight are tallied per batch, the rest add directly.
// Every one of them must come out right.
func TestWatchInbound_ManyAddressesCountExactly(t *testing.T) {
	w, _, f, _ := wrapped(t, profLinux)
	var srcs [][4]byte
	for i := 0; i < 12; i++ {
		a := [4]byte{100, 64, 7, byte(10 + i)}
		srcs = append(srcs, a)
		f.WatchInbound(addr(a))
	}
	var order [][4]byte
	for i, a := range srcs {
		for j := 0; j <= i; j++ { // address i appears i+1 times
			order = append(order, a)
		}
	}
	bufs, _ := batchFrom(order...)
	if len(bufs) > 128 {
		t.Fatalf("test batch too large: %d", len(bufs))
	}
	w.Write(bufs, wgOffset)
	for i, a := range srcs {
		if got := f.InboundCount(addr(a)); got != uint64(i+1) {
			t.Errorf("address %d: count %d, want %d", i, got, i+1)
		}
	}
}
