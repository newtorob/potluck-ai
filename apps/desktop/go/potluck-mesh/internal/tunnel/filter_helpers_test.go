package tunnel

import (
	"encoding/binary"
	"io"
	"log/slog"
	"net/netip"
	"os"
	"sync"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/tun"
)

// The filter logs every entry change; keep that out of the test output. Tests
// that look at the log install their own handler.
func TestMain(m *testing.M) {
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	os.Exit(m.Run())
}

// Addresses used across the filter tests.
var (
	selfA  = [4]byte{100, 64, 0, 1}
	hhA    = [4]byte{100, 64, 0, 2} // household peer
	hhB    = [4]byte{100, 64, 0, 3} // household peer
	circA  = [4]byte{100, 64, 1, 7} // restricted peer
	circB  = [4]byte{100, 64, 1, 8} // restricted peer
	strngr = [4]byte{100, 64, 9, 9} // in the mesh range, nobody's peer
)

func addr(a [4]byte) netip.Addr { return netip.AddrFrom4(a) }

// pkt describes an IPv4 packet to build. Zero values mean "an ordinary TCP
// packet with a 20 byte header and no payload".
type pkt struct {
	src, dst     [4]byte
	sport, dport uint16
	flags        byte
	proto        byte   // 0 means TCP (6)
	ihl          int    // header length in 32-bit words, 0 means 5
	frag         uint16 // flags and fragment offset field, raw
	payload      int
	total        int  // 0 means the real length; otherwise written into the total length field
	doff         byte // TCP data offset in words, 0 means 5
	doffRaw      byte // if non-zero, written into the data offset field after building, whatever the real header length
	cut          int  // if > 0, truncate the built packet to this many bytes
	version      byte // 0 means 4
	b0           byte // if non-zero, replaces the version and header length byte after building
}

func (p pkt) bytes() []byte {
	ihl := p.ihl
	if ihl == 0 {
		ihl = 5
	}
	proto := p.proto
	if proto == 0 {
		proto = 6
	}
	doff := p.doff
	if doff == 0 {
		doff = 5
	}
	hdr := ihl * 4
	b := make([]byte, hdr+int(doff)*4+p.payload)
	ver := p.version
	if ver == 0 {
		ver = 4
	}
	b[0] = ver<<4 | byte(ihl&0x0f)
	total := p.total
	if total == 0 {
		total = len(b)
	}
	binary.BigEndian.PutUint16(b[2:4], uint16(total))
	binary.BigEndian.PutUint16(b[6:8], p.frag)
	b[8] = 64
	b[9] = proto
	// Ports and flags first, addresses last, so a deliberately short header
	// length cannot garble the addresses a test is about.
	binary.BigEndian.PutUint16(b[hdr:hdr+2], p.sport)
	binary.BigEndian.PutUint16(b[hdr+2:hdr+4], p.dport)
	b[hdr+12] = doff << 4
	if p.doffRaw != 0 {
		b[hdr+12] = p.doffRaw << 4
	}
	b[hdr+13] = p.flags
	copy(b[12:16], p.src[:])
	copy(b[16:20], p.dst[:])
	if p.b0 != 0 {
		b[0] = p.b0
	}
	if p.cut > 0 && p.cut < len(b) {
		b = b[:p.cut]
	}
	return b
}

// TCP flag shorthands for the tests (tcpFIN etc. are the production names).
const (
	fSYN    = tcpSYN
	fSYNACK = tcpSYN | tcpACK
	fACK    = tcpACK
	fPSHACK = 0x08 | tcpACK
	fFINACK = tcpFIN | tcpACK
	fRSTACK = tcpRST | tcpACK
)

// fakeClock is a controllable clock: the monotonic and wall readings move
// separately, so a test can model a machine that sleeps (wall advances, mono
// does not) or a wall clock that is set back.
type fakeClock struct {
	mu         sync.Mutex
	mono, wall int64
}

func newFakeClock() *fakeClock {
	return &fakeClock{mono: int64(1000 * time.Hour), wall: time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC).UnixNano()}
}
func (c *fakeClock) read() (int64, int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mono, c.wall
}

// advance moves both clocks, like time passing normally.
func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	c.mono += int64(d)
	c.wall += int64(d)
	c.mu.Unlock()
}

// sleep moves only the wall clock, like a laptop lid closed for d.
func (c *fakeClock) sleep(d time.Duration) {
	c.mu.Lock()
	c.wall += int64(d)
	c.mu.Unlock()
}

// setBack moves only the wall clock backwards.
func (c *fakeClock) setBack(d time.Duration) {
	c.mu.Lock()
	c.wall -= int64(d)
	c.mu.Unlock()
}

// newTestFilter returns a filter with a fake clock, this host at selfA and the
// given household peers.
func newTestFilter(t testing.TB, household ...[4]byte) (*PacketFilter, *fakeClock) {
	t.Helper()
	set := map[[4]byte]bool{}
	for _, h := range household {
		set[h] = true
	}
	f := NewPacketFilter(func(a [4]byte) bool { return set[a] })
	clk := newFakeClock()
	f.now = clk.read
	f.SetSelf(addr(selfA).AsSlice())
	return f, clk
}

func mustEntry(t testing.TB, f *PacketFilter, peer [4]byte, in, out uint16, ttl time.Duration) {
	t.Helper()
	if err := f.SetEntry(addr(peer), Grant{InPort: in, OutPort: out, TTL: ttl}); err != nil {
		t.Fatalf("SetEntry(%v): %v", addr(peer), err)
	}
}

func (f *PacketFilter) in(p pkt) bool  { return f.verdict(p.bytes(), dirIn) }
func (f *PacketFilter) out(p pkt) bool { return f.verdict(p.bytes(), dirOut) }

// fromPeer / toPeer build packets between this host (selfA) and a peer.
func fromPeer(peer [4]byte, sport, dport uint16, flags byte) pkt {
	return pkt{src: peer, dst: selfA, sport: sport, dport: dport, flags: flags}
}
func toPeer(peer [4]byte, sport, dport uint16, flags byte) pkt {
	return pkt{src: selfA, dst: peer, sport: sport, dport: dport, flags: flags}
}

func dropCount(f *PacketFilter, reason string) uint64 { return f.Stats().Dropped[reason] }

// ---------------------------------------------------------------- fake TUN devices

// devProfile describes how one platform's wireguard-go TUN device behaves, as
// read from tun_linux.go, tun_darwin.go and tun_windows.go in the module cache.
type devProfile struct {
	name     string
	batch    int // BatchSize()
	headroom int // bytes in front of the packet the device itself uses (virtio header, AF header)
}

var (
	profLinux   = devProfile{"linux", 128, 10} // IFF_VNET_HDR: batch 128, 10 byte virtio_net_hdr before the packet
	profDarwin  = devProfile{"darwin", 1, 4}   // one packet per call, 4 byte AF header before the packet
	profWindows = devProfile{"windows", 1, 0}  // one packet per call, no header
)

const wgOffset = 16 // device.MessageTransportHeaderSize / MessageTransportOffsetContent

// fakeTUN records what the filter hands to the device and checks the buffer
// conventions wireguard-go relies on.
type fakeTUN struct {
	prof devProfile
	t    testing.TB

	mu       sync.Mutex
	incoming [][][]byte // batches Read will deliver, each element a bare packet
	readErr  error
	written  [][]byte // packets (bare, from offset) passed to Write, copied
	writeIdx []int    // for each written packet, the identity (pointer) check result
	calls    int      // Write calls
	lastArg  [][]byte // the exact slice passed to the last Write
	writeErr error
	closed   bool

	// Benchmarks: discard makes Write count nothing and keep nothing, and
	// readTemplate makes Read deliver the same batch every time.
	discard      bool
	readTemplate [][]byte
}

func newFakeTUN(t testing.TB, prof devProfile) *fakeTUN { return &fakeTUN{prof: prof, t: t} }

func (d *fakeTUN) File() *os.File           { return nil }
func (d *fakeTUN) MTU() (int, error)        { return 1280, nil }
func (d *fakeTUN) Name() (string, error)    { return "fake0", nil }
func (d *fakeTUN) Events() <-chan tun.Event { return nil }
func (d *fakeTUN) BatchSize() int           { return d.prof.batch }
func (d *fakeTUN) Close() error             { d.closed = true; return nil }

// Read delivers the next scripted batch: packet i is copied to bufs[i][offset:]
// and sizes[i] set, as the native devices do.
func (d *fakeTUN) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	if d.readTemplate != nil {
		for i, p := range d.readTemplate {
			sizes[i] = copy(bufs[i][offset:], p)
		}
		return len(d.readTemplate), nil
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if offset < d.prof.headroom {
		d.t.Errorf("%s: Read offset %d is smaller than the device header %d", d.prof.name, offset, d.prof.headroom)
	}
	if len(d.incoming) == 0 {
		return 0, d.readErr
	}
	batch := d.incoming[0]
	d.incoming = d.incoming[1:]
	if len(batch) > d.prof.batch || len(batch) > len(bufs) || len(sizes) < len(bufs) {
		d.t.Fatalf("%s: batch of %d does not fit (device batch %d, %d bufs, %d sizes)", d.prof.name, len(batch), d.prof.batch, len(bufs), len(sizes))
	}
	for i, p := range batch {
		sizes[i] = copy(bufs[i][offset:], p)
	}
	return len(batch), d.readErr
}

// Write checks the offset and the header room the way the native devices need
// it, and records the packets that arrive.
func (d *fakeTUN) Write(bufs [][]byte, offset int) (int, error) {
	if d.discard {
		return len(bufs), nil
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls++
	d.lastArg = bufs
	if offset < d.prof.headroom {
		d.t.Errorf("%s: Write offset %d is smaller than the device header %d", d.prof.name, offset, d.prof.headroom)
	}
	if len(bufs) > d.prof.batch {
		d.t.Errorf("%s: Write got %d packets, device batch is %d", d.prof.name, len(bufs), d.prof.batch)
	}
	for _, b := range bufs {
		if len(b) < offset {
			d.t.Errorf("%s: buffer shorter than the offset", d.prof.name)
			continue
		}
		// The device writes its header into the room before the packet. Doing
		// the same shows the filter did not move the start of the buffer.
		for i := offset - d.prof.headroom; i < offset; i++ {
			b[i] = 0xEE
		}
		d.written = append(d.written, append([]byte(nil), b[offset:]...))
	}
	if d.writeErr != nil {
		return 0, d.writeErr
	}
	return len(bufs), nil
}

// ---------------------------------------------------------------- batches

// writeBatch builds wireguard-go style buffers for Write: the packet sits after
// offset bytes, the buffer is exactly offset+len(packet) long, and the bytes in
// front are a marker the device must be able to overwrite.
func writeBatch(pkts ...pkt) [][]byte {
	out := make([][]byte, len(pkts))
	for i, p := range pkts {
		b := p.bytes()
		buf := make([]byte, wgOffset+len(b))
		for j := 0; j < wgOffset; j++ {
			buf[j] = 0xAA
		}
		copy(buf[wgOffset:], b)
		out[i] = buf
	}
	return out
}

// readBufs returns n full-size buffers and sizes, like RoutineReadFromTUN's.
func readBufs(n int) ([][]byte, []int) {
	bufs := make([][]byte, n)
	for i := range bufs {
		bufs[i] = make([]byte, 2048)
	}
	return bufs, make([]int, n)
}
