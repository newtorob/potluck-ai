package tunnel

import (
	"bytes"
	"encoding/base64"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
	"unsafe"

	"golang.zx2c4.com/wireguard/tun"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

var allProfiles = []devProfile{profLinux, profDarwin, profWindows}

// wrapped returns a filter-wrapped fake device of the given platform profile.
func wrapped(t testing.TB, prof devProfile, household ...[4]byte) (tun.Device, *fakeTUN, *PacketFilter, *fakeClock) {
	f, clk := newTestFilter(t, household...)
	d := newFakeTUN(t, prof)
	return f.Wrap(d), d, f, clk
}

func firstPtr(b []byte) uintptr { return uintptr(unsafe.Pointer(unsafe.SliceData(b))) }

// ---------------------------------------------------------------- Write

// When nothing is dropped, the device receives the caller's very slice, the
// buffers are not moved, and the offset is passed through.
func TestWrite_NothingDroppedIsPassedThroughUntouched(t *testing.T) {
	for _, prof := range allProfiles {
		t.Run(prof.name, func(t *testing.T) {
			w, d, _, _ := wrapped(t, prof, hhA, hhB)
			n := prof.batch
			var pk []pkt
			for i := 0; i < n; i++ {
				peer := hhA
				if i%2 == 1 {
					peer = hhB
				}
				pk = append(pk, pkt{src: peer, dst: selfA, sport: uint16(1000 + i), dport: 8321, flags: fPSHACK, payload: i % 50})
			}
			bufs := writeBatch(pk...)
			ptrs := make([]uintptr, n)
			lens := make([]int, n)
			caps := make([]int, n)
			for i, b := range bufs {
				ptrs[i], lens[i], caps[i] = firstPtr(b), len(b), cap(b)
			}
			got, err := w.Write(bufs, wgOffset)
			if err != nil || got != n {
				t.Fatalf("Write = %d, %v; want %d, nil", got, err, n)
			}
			if d.calls != 1 || len(d.lastArg) != n || firstPtr2(d.lastArg) != firstPtr2(bufs) {
				t.Errorf("the device did not get the caller's slice (calls=%d)", d.calls)
			}
			for i, b := range bufs {
				if firstPtr(b) != ptrs[i] || len(b) != lens[i] || cap(b) != caps[i] {
					t.Errorf("buffer %d was moved or resized", i)
				}
				if !bytes.Equal(b[wgOffset:], pk[i].bytes()) {
					t.Errorf("packet %d was changed", i)
				}
			}
			if len(d.written) != n {
				t.Errorf("device saw %d packets, want %d", len(d.written), n)
			}
		})
	}
}

func firstPtr2(b [][]byte) uintptr { return uintptr(unsafe.Pointer(unsafe.SliceData(b))) }

// Dropped packets are removed from the batch; the survivors keep their order,
// are the same buffers, and the caller's slice is left exactly as it was.
func TestWrite_DroppedPacketsLeaveTheBatch(t *testing.T) {
	for _, prof := range allProfiles {
		t.Run(prof.name, func(t *testing.T) {
			w, d, f, _ := wrapped(t, prof, hhA)
			mustEntry(t, f, circA, 8340, 0, time.Hour)
			n := prof.batch
			var pk []pkt
			var keep []int
			for i := 0; i < n; i++ {
				switch i % 4 {
				case 0: // household: kept
					pk = append(pk, pkt{src: hhA, dst: selfA, sport: uint16(i), dport: 22, flags: fSYN})
					keep = append(keep, i)
				case 1: // restricted peer to the granted port: kept
					pk = append(pk, pkt{src: circA, dst: selfA, sport: uint16(40000 + i), dport: 8340, flags: fSYN})
					keep = append(keep, i)
				case 2: // restricted peer, port 22: dropped
					pk = append(pk, pkt{src: circA, dst: selfA, sport: uint16(40000 + i), dport: 22, flags: fSYN})
				default: // stranger: dropped
					pk = append(pk, pkt{src: strngr, dst: selfA, sport: uint16(i), dport: 8340, flags: fSYN})
				}
			}
			if n == 1 {
				pk = []pkt{{src: circA, dst: selfA, sport: 40000, dport: 22, flags: fSYN}}
				keep = nil
			}
			bufs := writeBatch(pk...)
			orig := append([][]byte(nil), bufs...)
			origPtr := firstPtr2(bufs)
			got, err := w.Write(bufs, wgOffset)
			if err != nil {
				t.Fatalf("Write returned an error for dropped packets: %v", err)
			}
			if len(keep) == 0 {
				if d.calls != 0 || got != 0 {
					t.Errorf("all dropped: device called %d times, returned %d; want 0 calls", d.calls, got)
				}
			} else if d.calls != 1 || got != len(keep) {
				t.Errorf("calls=%d returned=%d; want 1 call, %d written", d.calls, got, len(keep))
			}
			if len(d.written) != len(keep) {
				t.Fatalf("device saw %d packets, want %d", len(d.written), len(keep))
			}
			for j, i := range keep {
				if !bytes.Equal(d.written[j], pk[i].bytes()) {
					t.Errorf("survivor %d is not packet %d", j, i)
				}
			}
			// The caller's slice and the buffers in it are exactly as they were.
			if firstPtr2(bufs) != origPtr || len(bufs) != n {
				t.Error("the caller's slice was replaced")
			}
			for i := range bufs {
				if &bufs[i][0] != &orig[i][0] || len(bufs[i]) != len(orig[i]) {
					t.Errorf("caller's bufs[%d] was swapped or resized", i)
				}
			}
			// What reached the device were the original buffers, not copies.
			for _, i := range keep {
				if bufs[i][wgOffset-prof.headroom] != 0xEE && prof.headroom > 0 {
					t.Errorf("survivor %d's header room was not the one the device wrote into", i)
				}
			}
			for i := range bufs {
				dropped := true
				for _, k := range keep {
					if k == i {
						dropped = false
					}
				}
				if dropped && bufs[i][0] != 0xAA {
					t.Errorf("a dropped buffer %d was handed to the device", i)
				}
			}
		})
	}
}

func TestWrite_DeviceErrorIsReturnedAsIs(t *testing.T) {
	for _, prof := range allProfiles {
		w, d, _, _ := wrapped(t, prof, hhA)
		d.writeErr = errors.New("device gone")
		_, err := w.Write(writeBatch(pkt{src: hhA, dst: selfA}), wgOffset)
		if err == nil || err.Error() != "device gone" {
			t.Errorf("%s: err = %v", prof.name, err)
		}
	}
}

func TestWrite_EmptyBatchAndShortBuffers(t *testing.T) {
	w, d, _, _ := wrapped(t, profLinux, hhA)
	if n, err := w.Write(nil, wgOffset); err != nil || n != 0 {
		t.Errorf("empty batch: %d, %v", n, err)
	}
	// A buffer with no packet bytes after the offset has nothing to judge; the
	// device sees what wireguard-go sent.
	b := make([]byte, wgOffset)
	if _, err := w.Write([][]byte{b}, wgOffset); err != nil {
		t.Errorf("short buffer: %v", err)
	}
	if len(d.written) != 1 {
		t.Errorf("a buffer the filter cannot judge was not passed on: %d", len(d.written))
	}
}

// ---------------------------------------------------------------- Read

func TestRead_DroppedPacketsGetSizeZero(t *testing.T) {
	for _, prof := range allProfiles {
		t.Run(prof.name, func(t *testing.T) {
			w, d, f, _ := wrapped(t, prof, hhA)
			mustEntry(t, f, circA, 8340, 9000, time.Hour)
			n := prof.batch
			var batch [][]byte
			wantPass := make([]bool, n)
			for i := 0; i < n; i++ {
				var p pkt
				switch i % 4 {
				case 0: // to a household peer
					p, wantPass[i] = pkt{src: selfA, dst: hhA, sport: uint16(i), dport: 22, flags: fSYN}, true
				case 1: // to the restricted peer's granted port
					p, wantPass[i] = pkt{src: selfA, dst: circA, sport: uint16(50000 + i), dport: 9000, flags: fSYN}, true
				case 2: // to the restricted peer, another port
					p = pkt{src: selfA, dst: circA, sport: uint16(50000 + i), dport: 22, flags: fSYN}
				default: // to a stranger
					p = pkt{src: selfA, dst: strngr, sport: uint16(i), dport: 9000, flags: fSYN}
				}
				batch = append(batch, p.bytes())
			}
			d.incoming = [][][]byte{batch}
			bufs, sizes := readBufs(n)
			got, err := w.Read(bufs, sizes, wgOffset)
			if err != nil || got != n {
				t.Fatalf("Read = %d, %v; want %d, nil", got, err, n)
			}
			for i := 0; i < n; i++ {
				if wantPass[i] {
					if sizes[i] != len(batch[i]) || !bytes.Equal(bufs[i][wgOffset:wgOffset+sizes[i]], batch[i]) {
						t.Errorf("packet %d should pass untouched (size %d)", i, sizes[i])
					}
				} else if sizes[i] != 0 {
					t.Errorf("packet %d should have size 0, has %d", i, sizes[i])
				}
			}
		})
	}
}

// wireguard-go processes the packets of a read that also returned an error
// (ErrTooManySegments on Linux), so the filter must too, and pass the result on.
func TestRead_ErrorAndPartialBatchPassThrough(t *testing.T) {
	w, d, f, _ := wrapped(t, profLinux, hhA)
	d.incoming = [][][]byte{{
		pkt{src: selfA, dst: hhA, flags: fSYN}.bytes(),
		pkt{src: selfA, dst: strngr, flags: fSYN}.bytes(),
	}}
	d.readErr = tun.ErrTooManySegments
	bufs, sizes := readBufs(128)
	n, err := w.Read(bufs, sizes, wgOffset)
	if n != 2 || err != tun.ErrTooManySegments {
		t.Fatalf("Read = %d, %v; want 2, ErrTooManySegments", n, err)
	}
	if sizes[0] == 0 || sizes[1] != 0 {
		t.Errorf("sizes = %v; want the household packet kept and the stranger's dropped", sizes[:2])
	}
	if dropCount(f, "not_a_peer") != 1 {
		t.Errorf("drops = %v", f.Stats().Dropped)
	}
	// Nothing read, error only.
	d.readErr = errors.New("closed")
	n, err = w.Read(bufs, sizes, wgOffset)
	if n != 0 || err == nil || err.Error() != "closed" {
		t.Errorf("Read at EOF = %d, %v", n, err)
	}
}

// Read must not touch a size that wireguard-go will skip, and must tolerate a
// device that reports a size larger than the buffer.
func TestRead_SizesItCannotJudgeAreLeftAlone(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	bufs, sizes := readBufs(3)
	sizes[0], sizes[1], sizes[2] = 0, -1, len(bufs[2])+5
	f.filterRead(bufs, sizes, wgOffset, 3)
	if sizes[0] != 0 || sizes[1] != -1 || sizes[2] != len(bufs[2])+5 {
		t.Errorf("sizes = %v", sizes)
	}
}

func TestWrapperForwardsTheDeviceInterface(t *testing.T) {
	for _, prof := range allProfiles {
		w, d, _, _ := wrapped(t, prof)
		if w.BatchSize() != prof.batch {
			t.Errorf("%s: BatchSize = %d, want %d", prof.name, w.BatchSize(), prof.batch)
		}
		if mtu, _ := w.MTU(); mtu != 1280 {
			t.Errorf("%s: MTU = %d", prof.name, mtu)
		}
		if name, _ := w.Name(); name != "fake0" {
			t.Errorf("%s: Name = %q", prof.name, name)
		}
		if err := w.Close(); err != nil || !d.closed {
			t.Errorf("%s: Close did not reach the device", prof.name)
		}
		_ = w.Events()
		_ = w.File()
	}
}

// ---------------------------------------------------------------- panic in the batch loops

func TestPanicDuringAWriteBatchDropsOnlyThatPacket(t *testing.T) {
	var calls atomic.Int32
	f := NewPacketFilter(func(a [4]byte) bool {
		if a == hhB {
			calls.Add(1)
			panic("household lookup blew up")
		}
		return a == hhA
	})
	f.SetSelf(addr(selfA).AsSlice())
	d := newFakeTUN(t, profLinux)
	w := f.Wrap(d)
	bufs := writeBatch(
		pkt{src: hhA, dst: selfA, sport: 1, dport: 22, flags: fSYN},
		pkt{src: hhB, dst: selfA, sport: 2, dport: 22, flags: fSYN}, // panics
		pkt{src: hhA, dst: selfA, sport: 3, dport: 22, flags: fSYN},
	)
	n, err := w.Write(bufs, wgOffset)
	if err != nil || n != 2 || len(d.written) != 2 {
		t.Fatalf("Write = %d, %v, device saw %d packets; want 2 survivors and no error", n, err, len(d.written))
	}
	if calls.Load() != 1 || dropCount(f, "panic") != 1 {
		t.Errorf("lookups=%d panic drops=%d", calls.Load(), dropCount(f, "panic"))
	}
	// And on read: size 0 for the packet that panicked, the others untouched.
	d.incoming = [][][]byte{{
		pkt{src: selfA, dst: hhA, flags: fSYN}.bytes(),
		pkt{src: selfA, dst: hhB, flags: fSYN}.bytes(),
	}}
	rb, rs := readBufs(128)
	if _, err := w.Read(rb, rs, wgOffset); err != nil || rs[0] == 0 || rs[1] != 0 {
		t.Errorf("Read after a lookup panic: sizes=%v err=%v", rs[:2], err)
	}
}

// ---------------------------------------------------------------- concurrency

// Several peers' receive loops call Write at once, the TUN reader calls Read,
// and entries come and go. The race detector is the main assertion; the rest
// checks nothing that must pass was dropped.
func TestConcurrentTrafficAndEntryChanges(t *testing.T) {
	f, _ := newTestFilter(t, hhA, hhB)
	d := newFakeTUN(t, profLinux)
	w := f.Wrap(d)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	var bad atomic.Int64

	for _, hh := range [][4]byte{hhA, hhB} {
		hh := hh
		wg.Add(1)
		go func() { // a household receive loop
			defer wg.Done()
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				bufs := writeBatch(
					pkt{src: hh, dst: selfA, sport: uint16(i), dport: 8321, flags: fACK},
					pkt{src: circA, dst: selfA, sport: 40000, dport: 22, flags: fSYN},
				)
				if n, err := w.Write(bufs, wgOffset); err != nil || n != 1 {
					bad.Add(1)
				}
			}
		}()
	}
	wg.Add(1)
	go func() { // the TUN reader
		defer wg.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			bufs, sizes := readBufs(2)
			d.mu.Lock()
			d.incoming = append(d.incoming, [][]byte{
				pkt{src: selfA, dst: hhA, sport: uint16(i), dport: 22, flags: fSYN}.bytes(),
				pkt{src: selfA, dst: circA, sport: 50000, dport: 9000, flags: fSYN}.bytes(),
			})
			d.mu.Unlock()
			w.Read(bufs, sizes, wgOffset)
			if sizes[0] == 0 {
				bad.Add(1)
			}
		}
	}()
	wg.Add(1)
	go func() { // the control side
		defer wg.Done()
		for i := 0; i < 2000; i++ {
			_ = f.SetEntry(addr(circA), Grant{InPort: 8340, OutPort: 9000, TTL: time.Hour})
			f.Entries()
			f.Stats()
			f.SetSelf(addr(selfA).AsSlice())
			if i%3 == 0 {
				f.RemoveEntry(addr(circA))
			}
		}
		close(stop)
	}()
	wg.Wait()
	if bad.Load() != 0 {
		t.Errorf("%d household packets were dropped or errored during entry changes", bad.Load())
	}
}

// The household set follows the peer store with no gap: the moment Merge or
// Reset has returned, packets of the added peer pass; the moment Remove has
// returned, they do not. A peer that stays in the store is never dropped while
// other peers come and go. Run with -race.
func TestHouseholdSetFollowsTheStoreUnderTraffic(t *testing.T) {
	st := peerstate.New()
	key := func(seed byte) string {
		return base64.StdEncoding.EncodeToString(bytes.Repeat([]byte{seed}, 32))
	}
	mk := func(id, ip string, seed byte) signal.RosterPeer {
		return signal.RosterPeer{NodeID: id, NodeName: id, MeshIP: ip, WGPublicKey: key(seed)}
	}
	stable := mk("stable", "100.64.0.20", 1)
	st.Reset("100.64.0.1", []signal.RosterPeer{stable})
	f := NewPacketFilter(st.IsHousehold)
	f.SetSelf(addr(selfA).AsSlice())

	stableAddr := [4]byte{100, 64, 0, 20}
	flapAddr := [4]byte{100, 64, 0, 21}
	var wg sync.WaitGroup
	stop := make(chan struct{})
	var stableDropped, lagged atomic.Int64
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				if !f.in(pkt{src: stableAddr, dst: selfA, sport: 1, dport: 2, flags: fACK}) ||
					!f.out(pkt{src: selfA, dst: stableAddr, sport: 1, dport: 2, flags: fACK}) {
					stableDropped.Add(1)
				}
				f.in(pkt{src: flapAddr, dst: selfA, sport: 1, dport: 2, flags: fACK}) // either answer is fine here
			}
		}()
	}
	for i := 0; i < 3000; i++ {
		flap := mk("flap", "100.64.0.21", 2)
		switch i % 3 {
		case 0:
			st.Merge([]signal.RosterPeer{flap}, nil)
		case 1:
			st.Reset("100.64.0.1", []signal.RosterPeer{stable, flap})
		default:
			st.Upsert(flap)
		}
		if !f.in(pkt{src: flapAddr, dst: selfA, sport: 1, dport: 2, flags: fACK}) || !f.out(pkt{src: selfA, dst: flapAddr, sport: 1, dport: 2, flags: fACK}) {
			lagged.Add(1) // the set did not include a peer whose Merge/Reset/Upsert had returned
		}
		st.Remove("flap")
		if f.in(pkt{src: flapAddr, dst: selfA, sport: 1, dport: 2, flags: fACK}) {
			lagged.Add(1) // the set still held a peer whose Remove had returned
		}
		if i%100 == 99 { // a welcome that drops the flapping peer
			st.Reset("100.64.0.1", []signal.RosterPeer{stable})
			if f.in(pkt{src: flapAddr, dst: selfA, sport: 1, dport: 2, flags: fACK}) {
				lagged.Add(1)
			}
		}
	}
	close(stop)
	wg.Wait()
	if stableDropped.Load() != 0 {
		t.Errorf("%d packets of a peer that was always in the store were dropped", stableDropped.Load())
	}
	if lagged.Load() != 0 {
		t.Errorf("the filter's view disagreed with the store %d times right after a store call returned", lagged.Load())
	}
}
