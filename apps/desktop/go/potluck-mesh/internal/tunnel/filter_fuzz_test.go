package tunnel

import (
	"encoding/binary"
	"math/rand"
	"sync/atomic"
	"testing"
	"time"
)

// FuzzVerdictInvariants feeds the filter arbitrary byte strings, carved into a
// sequence of packets, and checks the properties that must hold for any input:
//
//   - a packet whose peer end is a household peer passes;
//   - a packet whose peer end is some other address is dropped;
//   - a packet it cannot classify as IPv4 passes (wireguard-go decides);
//   - a packet that passes for the restricted peer is a well-formed TCP packet
//     with a 20 byte IP header, no fragmentation, the other end this host, and
//     either a SYN to the granted port for its direction or a packet of a
//     connection that an earlier passed SYN opened.
//
// "go test" runs the seed corpus. "go test -fuzz=FuzzVerdictInvariants" searches.
func FuzzVerdictInvariants(f *testing.F) {
	seq := func(items ...struct {
		dir uint8
		p   pkt
	}) []byte {
		var out []byte
		for _, it := range items {
			b := it.p.bytes()
			h := byte(len(b) - 20)
			if it.dir == dirIn {
				h |= 0x80
			}
			out = append(out, h)
			out = append(out, b...)
		}
		return out
	}
	type item = struct {
		dir uint8
		p   pkt
	}
	f.Add(seq(
		item{dirIn, fromPeer(circA, 40000, 8340, fSYN)},
		item{dirOut, toPeer(circA, 8340, 40000, fSYNACK)},
		item{dirIn, fromPeer(circA, 40000, 8340, fACK)},
		item{dirIn, fromPeer(circA, 40000, 22, fSYN)},
		item{dirOut, toPeer(circA, 50000, 9000, fSYN)},
		item{dirIn, fromPeer(circA, 9000, 50000, fSYNACK)},
		item{dirOut, toPeer(circA, 8340, 40000, fRSTACK)},
		item{dirIn, fromPeer(circA, 40000, 8340, fACK)},
	))
	f.Add(seq(
		item{dirIn, pkt{src: hhA, dst: selfA, proto: 17}},
		item{dirOut, pkt{src: selfA, dst: strngr, flags: fSYN}},
		item{dirIn, pkt{src: circA, dst: selfA, sport: 1, dport: 8340, flags: fSYN, frag: 0x2000}},
		item{dirIn, pkt{src: circA, dst: selfA, sport: 1, dport: 8340, flags: fSYN, ihl: 6, payload: 4}},
		item{dirIn, pkt{src: circA, dst: hhA, sport: 1, dport: 8340, flags: fSYN}},
	))
	f.Add([]byte{0x80, 0x45, 0x00})
	f.Add([]byte{})

	f.Fuzz(func(t *testing.T, data []byte) {
		checkSequence(t, data)
	})
}

// checkSequence runs one carved sequence against a fresh filter and checks the invariants.
func checkSequence(t *testing.T, data []byte) {
	flt, clk := newTestFilter(t, hhA, hhB)
	flt.SetEntry(addr(circA), Grant{InPort: 8340, OutPort: 9000, TTL: time.Hour})
	opened := map[uint32]bool{}
	for len(data) > 0 {
		h := data[0]
		dir := dirOut
		if h&0x80 != 0 {
			dir = dirIn
		}
		n := int(h&0x7f) + 20
		data = data[1:]
		if n > len(data) {
			n = len(data)
		}
		p := data[:n]
		data = data[n:]
		clk.advance(time.Second)

		pass := flt.verdict(p, dir)
		if len(p) < 20 || p[0]>>4 != 4 {
			if !pass {
				t.Fatalf("unclassifiable packet (%d bytes, first byte %#x) was dropped", len(p), firstByte(p))
			}
			continue
		}
		var peer [4]byte
		if dir == dirIn {
			peer = [4]byte(p[12:16])
		} else {
			peer = [4]byte(p[16:20])
		}
		switch peer {
		case hhA, hhB:
			if !pass {
				t.Fatalf("household packet dropped: dir=%d % x", dir, p)
			}
		case circA:
			if pass {
				assertRestrictedShape(t, p, dir, opened)
			}
		default:
			if pass {
				t.Fatalf("packet for non-peer %v passed: dir=%d % x", addr(peer), dir, p)
			}
		}
	}
}

func firstByte(p []byte) byte {
	if len(p) == 0 {
		return 0
	}
	return p[0]
}

// restrictedPassesChecked counts the passed restricted packets the invariants
// were applied to, so a search that never gets a packet through cannot look
// like a pass.
var restrictedPassesChecked atomic.Int64

// assertRestrictedShape checks a packet the filter passed for the restricted peer.
func assertRestrictedShape(t *testing.T, p []byte, dir uint8, opened map[uint32]bool) {
	t.Helper()
	restrictedPassesChecked.Add(1)
	bad := func(why string) { t.Fatalf("passed a restricted packet that %s: dir=%d % x", why, dir, p) }
	if p[0] != 0x45 {
		bad("has IP options or a bad header length")
	}
	total := int(binary.BigEndian.Uint16(p[2:4]))
	if total < 40 || total > len(p) {
		bad("has an inconsistent total length")
	}
	if binary.BigEndian.Uint16(p[6:8])&0x3fff != 0 {
		bad("is a fragment")
	}
	if p[9] != 6 {
		bad("is not TCP")
	}
	var other [4]byte
	if dir == dirIn {
		other = [4]byte(p[16:20])
	} else {
		other = [4]byte(p[12:16])
	}
	if other != selfA {
		bad("does not involve this host")
	}
	tcp := p[20:total]
	if doff := int(tcp[12]>>4) << 2; doff < 20 || doff > len(tcp) {
		bad("has a bad TCP header length")
	}
	sport, dport, flags := binary.BigEndian.Uint16(tcp[0:2]), binary.BigEndian.Uint16(tcp[2:4]), tcp[13]
	var key uint32
	var granted uint16
	if dir == dirIn {
		key, granted = uint32(sport)<<16|uint32(dport), 8340
	} else {
		key, granted = uint32(dport)<<16|uint32(sport), 9000
	}
	if flags&(tcpSYN|tcpACK|tcpRST|tcpFIN) == tcpSYN {
		if dport != granted {
			bad("opens a connection to a port that was not granted")
		}
		opened[key] = true
		return
	}
	if !opened[key] {
		bad("belongs to no connection that an earlier SYN opened")
	}
}

// A seeded random search over mutations of valid packets, so the invariants are
// exercised well beyond the seed corpus on every run of the tests.
func TestVerdictInvariantsOnMutatedPackets(t *testing.T) {
	restrictedPassesChecked.Store(0)
	defer func() {
		if n := restrictedPassesChecked.Load(); n < 2000 {
			t.Errorf("only %d passed restricted packets were checked; the search is not reaching the rules", n)
		} else {
			t.Logf("checked %d passed restricted packets", n)
		}
	}()
	rng := rand.New(rand.NewSource(20261009))
	bases := []pkt{
		fromPeer(circA, 40000, 8340, fSYN), toPeer(circA, 8340, 40000, fSYNACK), fromPeer(circA, 40000, 8340, fACK),
		toPeer(circA, 50000, 9000, fSYN), fromPeer(circA, 9000, 50000, fSYNACK), fromPeer(circA, 40000, 22, fSYN),
		{src: hhA, dst: selfA, sport: 1, dport: 2, flags: fSYN}, {src: selfA, dst: hhB, proto: 17},
		{src: strngr, dst: selfA, flags: fSYN}, fromPeer(circA, 1, 8340, fSYN),
	}
	for round := 0; round < 30000; round++ {
		var data []byte
		for k := 0; k < 1+rng.Intn(12); k++ {
			b := bases[rng.Intn(len(bases))].bytes()
			// mutate a few bytes, sometimes the header length, flags or lengths
			for m := 0; m < rng.Intn(4); m++ {
				switch rng.Intn(6) {
				case 0:
					b[rng.Intn(len(b))] = byte(rng.Intn(256))
				case 1:
					b[0] = byte(0x40 | rng.Intn(16))
				case 2:
					if len(b) > 33 {
						b[33] = byte(rng.Intn(256)) // TCP flags
					}
				case 3:
					binary.BigEndian.PutUint16(b[2:4], uint16(rng.Intn(80)))
				case 4:
					binary.BigEndian.PutUint16(b[6:8], uint16(rng.Intn(1<<16)))
				case 5:
					b = b[:20+rng.Intn(len(b)-19)]
				}
			}
			h := byte(len(b)-20) & 0x7f
			if rng.Intn(2) == 0 {
				h |= 0x80
			}
			data = append(data, h)
			data = append(data, b...)
		}
		checkSequence(t, data)
	}
}
