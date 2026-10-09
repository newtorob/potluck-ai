package tunnel

import (
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// benchStore is a peer store holding n household peers at 100.64.0.2 and up,
// the way the filter sees it in production.
func benchStore(b testing.TB, n int) *peerstate.Store {
	st := peerstate.New()
	var peers []signal.RosterPeer
	for i := 0; i < n; i++ {
		peers = append(peers, signal.RosterPeer{
			NodeID:      string(rune('a'+i%26)) + string(rune('a'+i/26)),
			MeshIP:      addr([4]byte{100, 64, 0, byte(2 + i)}).String(),
			WGPublicKey: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		})
	}
	st.Reset("100.64.0.1", peers)
	return st
}

// The household fast path: what every packet of an existing mesh pays.
func BenchmarkHouseholdPacket(b *testing.B) {
	for _, n := range []int{3, 50} {
		b.Run(map[int]string{3: "3peers", 50: "50peers"}[n], func(b *testing.B) {
			st := benchStore(b, n)
			f := NewPacketFilter(st.IsHousehold)
			f.SetSelf(addr(selfA).AsSlice())
			in := pkt{src: [4]byte{100, 64, 0, 3}, dst: selfA, sport: 40000, dport: 8321, flags: fPSHACK, payload: 1200}.bytes()
			out := pkt{src: selfA, dst: [4]byte{100, 64, 0, 3}, sport: 8321, dport: 40000, flags: fPSHACK, payload: 1200}.bytes()
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if !f.verdict(in, dirIn) || !f.verdict(out, dirOut) {
					b.Fatal("dropped")
				}
			}
			b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(2*b.N), "ns/pkt")
		})
	}
}

// The same household packet once circle peers exist: one more map lookup, in
// the table of restricted peers, before the household set answers.
func BenchmarkHouseholdPacketWithRestrictedEntries(b *testing.B) {
	st := benchStore(b, 3)
	f := NewPacketFilter(st.IsHousehold)
	f.SetSelf(addr(selfA).AsSlice())
	f.SetEntry(addr(circA), Grant{InPort: 8340, TTL: time.Hour})
	f.SetEntry(addr(circB), Grant{OutPort: 9000, TTL: time.Hour})
	in := pkt{src: [4]byte{100, 64, 0, 3}, dst: selfA, sport: 40000, dport: 8321, flags: fPSHACK, payload: 1200}.bytes()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if !f.verdict(in, dirIn) {
			b.Fatal("dropped")
		}
	}
	b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(b.N), "ns/pkt")
}

func BenchmarkHouseholdPacketParallel(b *testing.B) {
	st := benchStore(b, 3)
	f := NewPacketFilter(st.IsHousehold)
	f.SetSelf(addr(selfA).AsSlice())
	in := pkt{src: [4]byte{100, 64, 0, 3}, dst: selfA, sport: 40000, dport: 8321, flags: fPSHACK, payload: 1200}.bytes()
	b.ReportAllocs()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			if !f.verdict(in, dirIn) {
				b.Fatal("dropped")
			}
		}
	})
}

// A full Linux-size batch through the wrapper, all household: the cost the
// wrapper adds to a Write or Read of 128 packets.
func BenchmarkHouseholdBatch128(b *testing.B) {
	st := benchStore(b, 3)
	f := NewPacketFilter(st.IsHousehold)
	f.SetSelf(addr(selfA).AsSlice())
	d := newFakeTUN(b, profLinux)
	d.discard = true
	w := f.Wrap(d)
	var pk []pkt
	for i := 0; i < 128; i++ {
		pk = append(pk, pkt{src: [4]byte{100, 64, 0, 3}, dst: selfA, sport: uint16(40000 + i), dport: 8321, flags: fPSHACK, payload: 1200})
	}
	bufs := writeBatch(pk...)
	b.Run("write", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			w.Write(bufs, wgOffset)
		}
		b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
	})
	// The inbound watch (filter_watch.go) on the same batch: the packets' source is watched, other
	// addresses are watched, or eight other addresses are watched.
	watchRun := func(name string, watched ...[4]byte) {
		b.Run(name, func(b *testing.B) {
			for _, a := range watched {
				f.WatchInbound(addr(a))
			}
			defer func() {
				for _, a := range watched {
					f.UnwatchInbound(addr(a))
				}
			}()
			b.ReportAllocs()
			for i := 0; i < b.N; i++ {
				w.Write(bufs, wgOffset)
			}
			b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
		})
	}
	watchRun("write-watching-the-source", [4]byte{100, 64, 0, 3})
	watchRun("write-watching-another-address", [4]byte{100, 64, 0, 9})
	watchRun("write-watching-8-other-addresses", [4]byte{100, 64, 0, 9}, [4]byte{100, 64, 0, 10}, [4]byte{100, 64, 0, 11}, [4]byte{100, 64, 0, 12},
		[4]byte{100, 64, 0, 13}, [4]byte{100, 64, 0, 14}, [4]byte{100, 64, 0, 15}, [4]byte{100, 64, 0, 16})
	b.Run("write-baseline-no-filter", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			d.Write(bufs, wgOffset)
		}
		b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
	})
	b.Run("write-one-drop-in-the-batch", func(b *testing.B) {
		bad := writeBatch(append([]pkt{{src: strngr, dst: selfA, flags: fSYN}}, pk[1:]...)...)
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			w.Write(bad, wgOffset)
		}
		b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
	})
	b.Run("read", func(b *testing.B) {
		rb, rs := readBufs(128)
		d.readTemplate = make([][]byte, 128)
		for i := range d.readTemplate {
			d.readTemplate[i] = pkt{src: selfA, dst: [4]byte{100, 64, 0, 3}, sport: uint16(8321), dport: uint16(40000 + i), flags: fPSHACK, payload: 1200}.bytes()
		}
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			w.Read(rb, rs, wgOffset)
		}
		b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
	})
	b.Run("read-baseline-no-filter", func(b *testing.B) {
		rb, rs := readBufs(128)
		b.ReportAllocs()
		b.ResetTimer()
		for i := 0; i < b.N; i++ {
			d.Read(rb, rs, wgOffset)
		}
		b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(128*b.N), "ns/pkt")
	})
}

// A restricted peer's packet on an established flow: the entry lock, the clock
// read and the flow table.
func BenchmarkRestrictedPacket(b *testing.B) {
	f := NewPacketFilter(nil)
	f.SetSelf(addr(selfA).AsSlice())
	if err := f.SetEntry(addr(circA), Grant{InPort: 8340, OutPort: 9000, TTL: time.Hour}); err != nil {
		b.Fatal(err)
	}
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 50000, 9000, fSYN))
	in := fromPeer(circA, 40000, 8340, fPSHACK)
	in.payload = 1200
	out := toPeer(circA, 50000, 9000, fPSHACK)
	out.payload = 1200
	inB, outB := in.bytes(), out.bytes()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if !f.verdict(inB, dirIn) || !f.verdict(outB, dirOut) {
			b.Fatal("dropped")
		}
	}
	b.ReportMetric(float64(b.Elapsed().Nanoseconds())/float64(2*b.N), "ns/pkt")
}

// Dropped packets: an unknown address, and a restricted peer's forbidden port.
func BenchmarkDroppedPacket(b *testing.B) {
	f := NewPacketFilter(nil)
	f.SetSelf(addr(selfA).AsSlice())
	f.SetEntry(addr(circA), Grant{InPort: 8340, TTL: time.Hour})
	stranger := pkt{src: [4]byte{192, 168, 5, 5}, dst: selfA, sport: 1, dport: 2, flags: fSYN}.bytes() // not in the mesh range: no log
	forbidden := fromPeer(circA, 40000, 22, fSYN).bytes()
	b.Run("unknown-address", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			f.verdict(stranger, dirIn)
		}
	})
	b.Run("restricted-forbidden-port", func(b *testing.B) {
		b.ReportAllocs()
		for i := 0; i < b.N; i++ {
			f.verdict(forbidden, dirIn)
		}
	})
}
