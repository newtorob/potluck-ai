package tunnel

import (
	"bytes"
	"fmt"
	"log/slog"
	"net/netip"
	"runtime"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------- household

// With no restricted peers the filter must be invisible to household traffic:
// every protocol, every shape of packet, both directions.
func TestHouseholdPassesEverythingBothDirections(t *testing.T) {
	f, _ := newTestFilter(t, hhA, hhB)
	shapes := map[string]pkt{
		"tcp syn":            {sport: 1234, dport: 8321, flags: fSYN},
		"tcp data":           {sport: 1234, dport: 22, flags: fPSHACK, payload: 100},
		"tcp rst":            {sport: 1, dport: 2, flags: fRSTACK},
		"tcp no flow at all": {sport: 50052, dport: 40000, flags: fACK},
		"udp":                {proto: 17, sport: 5353, dport: 5353, payload: 40},
		"icmp":               {proto: 1, payload: 40},
		"gre":                {proto: 47, payload: 40},
		"first fragment":     {frag: 0x2000, payload: 100},
		"later fragment":     {frag: 0x00b9, payload: 100},
		"ip options":         {ihl: 7, sport: 1, dport: 2, flags: fSYN, payload: 8},
		"tcp doff garbage":   {doff: 15, flags: fSYN},
		"ihl below minimum":  {ihl: 3},
		"total length lies":  {total: 9000, payload: 10},
		"total length zero":  {total: 1, payload: 10},
		"bare 20 byte hdr":   {cut: 20},
		"proto 0":            {proto: 255},
	}
	for name, base := range shapes {
		for _, peer := range [][4]byte{hhA, hhB} {
			in := base
			in.src, in.dst = peer, selfA
			if !f.in(in) {
				t.Errorf("%s: household %v -> host was dropped", name, addr(peer))
			}
			// the shape is unrestricted whoever the host side is, too
			out := base
			out.src, out.dst = selfA, peer
			if !f.out(out) {
				t.Errorf("%s: host -> household %v was dropped", name, addr(peer))
			}
			odd := base
			odd.src, odd.dst = [4]byte{192, 168, 1, 5}, peer // a forwarded packet toward a household peer
			if !f.out(odd) {
				t.Errorf("%s: forwarded packet toward household %v was dropped", name, addr(peer))
			}
		}
	}
	if n := f.Stats().Total(); n != 0 {
		t.Errorf("household traffic caused %d drops: %v", n, f.Stats().Dropped)
	}
}

// With nothing registered, an address that is not a household peer is dropped
// in both directions. wireguard-go never delivers such a packet today.
func TestNoEntries_NonHouseholdIsDropped(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	others := [][4]byte{circA, strngr, {224, 0, 0, 251}, {255, 255, 255, 255}, {127, 0, 0, 1}, {0, 0, 0, 0}, {192, 168, 1, 1}, {100, 64, 0, 255}}
	for _, o := range others {
		if f.in(pkt{src: o, dst: selfA, sport: 1, dport: 2, flags: fSYN}) {
			t.Errorf("packet from %v passed", addr(o))
		}
		if f.out(pkt{src: selfA, dst: o, sport: 1, dport: 2, flags: fSYN}) {
			t.Errorf("packet to %v passed", addr(o))
		}
	}
	if got, want := dropCount(f, "not_a_peer"), uint64(2*len(others)); got != want {
		t.Errorf("not_a_peer drops = %d, want %d", got, want)
	}
	if len(f.Entries()) != 0 {
		t.Errorf("entries = %v, want none", f.Entries())
	}
}

// The household side of a packet is the PEER end: the destination going out,
// the source coming in. The other end does not matter for a household peer.
func TestPeerEndIsDestinationOutAndSourceIn(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	// source is household, destination is not, going OUT: the peer end is the destination.
	if f.out(pkt{src: hhA, dst: strngr, flags: fSYN}) {
		t.Error("outbound packet to a non-peer passed because its source is a household address")
	}
	// destination is household, source is not, coming IN: the peer end is the source.
	if f.in(pkt{src: strngr, dst: hhA, flags: fSYN}) {
		t.Error("inbound packet from a non-peer passed because its destination is a household address")
	}
}

// What the filter does with packets it cannot classify as IPv4: it leaves them
// to wireguard-go. This pins that, including for malformed packets whose
// version nibble says 4 but which are too short to carry addresses.
func TestUnclassifiablePassesThrough(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	v6 := make([]byte, 60)
	v6[0] = 6 << 4
	cases := map[string][]byte{
		"ipv6":                          v6,
		"ipv6 header only":              v6[:40],
		"ipv6 one byte":                 {6 << 4},
		"version 0":                     make([]byte, 40),
		"version 5":                     append([]byte{5 << 4}, make([]byte, 39)...),
		"version 15":                    append([]byte{0xf0}, make([]byte, 39)...),
		"ipv4 nibble, 19 bytes":         append([]byte{0x45}, make([]byte, 18)...),
		"ipv4 nibble, 1 byte":           {0x45},
		"empty":                         {},
		"nil":                           nil,
		"ipv4 nibble, ihl 15, 19 bytes": append([]byte{0x4f}, make([]byte, 18)...),
	}
	for name, b := range cases {
		if !f.verdict(b, dirIn) || !f.verdict(b, dirOut) {
			t.Errorf("%s: dropped, want passed through untouched", name)
		}
	}
	if n := f.Stats().Total(); n != 0 {
		t.Errorf("unclassifiable packets were counted as drops: %v", f.Stats().Dropped)
	}
}

// An entry wins over household membership: an address in both is restricted.
func TestEntryWinsOverHousehold(t *testing.T) {
	f, _ := newTestFilter(t, hhA, hhB)
	if !f.in(fromPeer(hhA, 5, 22, fSYN)) {
		t.Fatal("household peer should pass before it has an entry")
	}
	mustEntry(t, f, hhA, 8340, 0, time.Hour)
	if f.in(fromPeer(hhA, 5, 22, fSYN)) {
		t.Error("restricted household address reached port 22")
	}
	if !f.in(fromPeer(hhA, 5, 8340, fSYN)) {
		t.Error("restricted household address could not reach its granted port")
	}
	if !f.in(fromPeer(hhB, 5, 22, fSYN)) {
		t.Error("another household peer was affected")
	}
	f.RemoveEntry(addr(hhA))
	if !f.in(fromPeer(hhA, 6, 22, fSYN)) {
		t.Error("after RemoveEntry the household peer is unrestricted again")
	}
}

// ---------------------------------------------------------------- restricted: ports

func TestRestricted_InboundPortAndReplies(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 8340, 0, time.Hour)

	if !f.in(fromPeer(circA, 40000, 8340, fSYN)) {
		t.Fatal("SYN to the granted port was dropped")
	}
	if !f.out(toPeer(circA, 8340, 40000, fSYNACK)) {
		t.Error("our SYN-ACK was dropped")
	}
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) || !f.in(fromPeer(circA, 40000, 8340, fPSHACK)) {
		t.Error("peer ACK/data on the tracked flow was dropped")
	}
	if !f.out(toPeer(circA, 8340, 40000, fPSHACK)) {
		t.Error("our data on the tracked flow was dropped")
	}

	// Another port, from the same source port.
	if f.in(fromPeer(circA, 40000, 8341, fSYN)) {
		t.Error("SYN to a port that was not granted passed")
	}
	if f.in(fromPeer(circA, 40000, 22, fSYN)) {
		t.Error("SYN to 22 passed")
	}
	// The granted port used as a SOURCE port toward a different port: the old
	// prototype let this through as a "reply".
	if f.in(fromPeer(circA, 8340, 22, fACK)) || f.in(fromPeer(circA, 8340, 22, fSYN)) || f.in(fromPeer(circA, 8340, 22, fSYNACK)) {
		t.Error("granted port as source port reached port 22")
	}
	// Non-SYN packets of no tracked flow, whatever they look like.
	for name, fl := range map[string]byte{"ack": fACK, "psh-ack": fPSHACK, "fin-ack": fFINACK, "rst": tcpRST, "rst-ack": fRSTACK, "syn-ack": fSYNACK, "no flags": 0} {
		if f.in(fromPeer(circA, 41000, 8340, byte(fl))) {
			t.Errorf("%s of no tracked flow passed", name)
		}
	}
	// A packet of the tracked flow but with another port pair.
	if f.in(fromPeer(circA, 40001, 8340, fACK)) {
		t.Error("ACK from another source port passed on the strength of the flow from 40000")
	}
	// The tracked flow still works after all of that.
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("the tracked flow was damaged by blocked packets")
	}
	// This host cannot open connections to the peer: the grant is inbound only.
	if f.out(toPeer(circA, 50000, 9000, fSYN)) {
		t.Error("outbound SYN passed on an inbound-only grant")
	}
}

func TestRestricted_OutboundPortAndReplies(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 0, 9000, time.Hour)

	if !f.out(toPeer(circA, 50000, 9000, fSYN)) {
		t.Fatal("outbound SYN to the granted port was dropped")
	}
	if !f.in(fromPeer(circA, 9000, 50000, fSYNACK)) {
		t.Error("the peer's SYN-ACK was dropped")
	}
	if !f.out(toPeer(circA, 50000, 9000, fACK)) || !f.in(fromPeer(circA, 9000, 50000, fPSHACK)) {
		t.Error("data on the tracked flow was dropped")
	}
	if f.out(toPeer(circA, 50000, 9001, fSYN)) {
		t.Error("outbound SYN to another port passed")
	}
	// A reply-shaped packet from the granted port to a port we never used.
	if f.in(fromPeer(circA, 9000, 50001, fSYNACK)) || f.in(fromPeer(circA, 9000, 50001, fACK)) {
		t.Error("reply-shaped packet with no tracked flow passed")
	}
	// The peer cannot open a connection to us on an outbound-only grant.
	if f.in(fromPeer(circA, 40000, 8340, fSYN)) || f.in(fromPeer(circA, 40000, 22, fSYN)) {
		t.Error("inbound SYN passed on an outbound-only grant")
	}
}

func TestRestricted_BothDirectionsOnOnePeer(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	if !f.in(fromPeer(circA, 40000, 8340, fSYN)) || !f.out(toPeer(circA, 50000, 9000, fSYN)) {
		t.Fatal("a SYN in each granted direction should pass")
	}
	if !f.out(toPeer(circA, 8340, 40000, fSYNACK)) || !f.in(fromPeer(circA, 9000, 50000, fSYNACK)) {
		t.Error("replies on the two flows were dropped")
	}
	if f.in(fromPeer(circA, 40000, 9000, fSYN)) || f.out(toPeer(circA, 50000, 8340, fSYN)) {
		t.Error("each direction's port must not be usable in the other direction")
	}
}

// The inbound destination must be this host, and outbound the source must be.
func TestRestricted_OtherEndMustBeThisHost(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	// inbound to a household machine behind this host (forwarding), same port
	if f.in(pkt{src: circA, dst: hhA, sport: 40000, dport: 8340, flags: fSYN}) {
		t.Error("inbound SYN addressed to a household peer passed")
	}
	// inbound to some other address
	if f.in(pkt{src: circA, dst: strngr, sport: 40000, dport: 8340, flags: fSYN}) {
		t.Error("inbound SYN addressed to a stranger passed")
	}
	// outbound with a source that is not this host (forwarded household traffic)
	if f.out(pkt{src: hhA, dst: circA, sport: 50000, dport: 9000, flags: fSYN}) {
		t.Error("outbound SYN forwarded from a household peer passed")
	}
	if dropCount(f, "wrong_host") != 3 {
		t.Errorf("wrong_host drops = %d, want 3 (%v)", dropCount(f, "wrong_host"), f.Stats().Dropped)
	}
}

func TestRestricted_NeedsSelfAddress(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	f.SetSelf(nil)
	if f.in(fromPeer(circA, 1, 8340, fSYN)) || f.out(toPeer(circA, 1, 9000, fSYN)) {
		t.Error("restricted traffic passed while this host's address is unknown")
	}
	if !f.in(pkt{src: hhA, dst: selfA, flags: fSYN}) {
		t.Error("household traffic must not depend on knowing the host address")
	}
	if dropCount(f, "no_self_address") != 2 {
		t.Errorf("no_self_address drops = %d, want 2", dropCount(f, "no_self_address"))
	}
	// A new address: flows opened under the old one stop matching.
	f.SetSelf(addr(selfA).AsSlice())
	if !f.in(fromPeer(circA, 1, 8340, fSYN)) {
		t.Fatal("SYN should pass once the address is known")
	}
	f.SetSelf(addr([4]byte{100, 64, 0, 99}).AsSlice())
	if f.in(fromPeer(circA, 1, 8340, fACK)) {
		t.Error("a packet addressed to the old host address passed after the address changed")
	}
}

// ---------------------------------------------------------------- restricted: what is not TCP to the port

func TestRestricted_ProtocolsAndShapes(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	// A tracked flow exists, so the drops below are about the packet, not a missing flow.
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 50000, 9000, fSYN))

	type tc struct {
		name   string
		p      pkt
		reason string
	}
	for _, c := range []tc{
		{"udp to the granted port", pkt{proto: 17, sport: 40000, dport: 8340, payload: 20}, "not_tcp"},
		{"icmp echo", pkt{proto: 1, payload: 8}, "not_tcp"},
		{"icmp echo as the harness sends it (36 bytes)", pkt{proto: 1, total: 36, cut: 36}, "not_tcp"},
		{"udp datagram as the harness sends it (36 bytes)", pkt{proto: 17, total: 36, cut: 36}, "not_tcp"},
		{"later fragment of 36 bytes", pkt{frag: 0x0003, total: 36, cut: 36}, "fragment"},
		{"36 byte tcp packet (no room for a TCP header)", pkt{sport: 40000, dport: 8340, flags: fACK, total: 36, cut: 36}, "malformed"},
		{"total length 10", pkt{proto: 1, total: 10}, "malformed"},
		{"gre", pkt{proto: 47, payload: 20}, "not_tcp"},
		{"sctp", pkt{proto: 132, payload: 20}, "not_tcp"},
		{"first fragment of a SYN", pkt{sport: 40002, dport: 8340, flags: fSYN, frag: 0x2000}, "fragment"},
		{"first fragment on a tracked flow", pkt{sport: 40000, dport: 8340, flags: fACK, frag: 0x2000}, "fragment"},
		{"later fragment", pkt{sport: 40000, dport: 8340, flags: fACK, frag: 0x0010, payload: 20}, "fragment"},
		{"fragment offset 1 with DF", pkt{sport: 40000, dport: 8340, flags: fACK, frag: 0x4001}, "fragment"},
		{"ip options", pkt{ihl: 6, sport: 40000, dport: 8340, flags: fACK, payload: 4}, "ip_options"},
		{"ip options, header 60 bytes", pkt{ihl: 15, sport: 40000, dport: 8340, flags: fACK}, "ip_options"},
		{"ihl 4", pkt{ihl: 4, payload: 4, sport: 40000, dport: 8340, flags: fACK}, "malformed"},
		{"ihl nibble 4 on an otherwise valid SYN", pkt{b0: 0x44, sport: 40010, dport: 8340, flags: fSYN}, "malformed"},
		{"ihl nibble 3 on an otherwise valid SYN", pkt{b0: 0x43, sport: 40010, dport: 8340, flags: fSYN}, "malformed"},
		{"ihl nibble 0 on an otherwise valid SYN", pkt{b0: 0x40, sport: 40010, dport: 8340, flags: fSYN}, "malformed"},
		{"ihl nibble 6 on an otherwise valid SYN", pkt{b0: 0x46, sport: 40010, dport: 8340, flags: fSYN}, "ip_options"},
		{"tcp header cut at 39 bytes", pkt{sport: 40000, dport: 8340, flags: fACK, cut: 39}, "malformed"},
		{"tcp header cut at 21 bytes", pkt{sport: 40000, dport: 8340, flags: fACK, cut: 21}, "malformed"},
		{"total length shorter than headers", pkt{sport: 40000, dport: 8340, flags: fACK, total: 30, payload: 10}, "malformed"},
		{"total length longer than the packet", pkt{sport: 40000, dport: 8340, flags: fACK, total: 1500}, "malformed"},
		{"tcp data offset 4", pkt{sport: 40000, dport: 8340, flags: fACK, payload: 4, doffRaw: 4}, "malformed"},
		{"tcp data offset beyond the packet", pkt{sport: 40000, dport: 8340, flags: fACK, doffRaw: 15}, "malformed"},
	} {
		before := dropCount(f, c.reason)
		p := c.p
		p.src, p.dst = circA, selfA
		if f.in(p) {
			t.Errorf("%s: inbound packet passed", c.name)
		}
		if got := dropCount(f, c.reason); got != before+1 {
			t.Errorf("%s: want a %s drop, counters %v", c.name, c.reason, f.Stats().Dropped)
		}
		// and the same shapes toward the peer
		q := c.p
		q.src, q.dst = selfA, circA
		q.sport, q.dport = 50000, 9000
		if f.out(q) {
			t.Errorf("%s: outbound packet passed", c.name)
		}
	}
	// The tracked flows survived all of that.
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) || !f.out(toPeer(circA, 50000, 9000, fACK)) {
		t.Error("tracked flows were damaged by dropped packets")
	}
}

// A SYN to port 0 must not open a flow on a direction that has no grant (0 means
// "none", not "port 0").
func TestRestricted_PortZeroIsNotAGrant(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	if f.out(toPeer(circA, 50000, 0, fSYN)) {
		t.Error("outbound SYN to port 0 passed on an entry with no outbound grant")
	}
	mustEntry(t, f, circB, 0, 9000, time.Hour)
	if f.in(fromPeer(circB, 40000, 0, fSYN)) {
		t.Error("inbound SYN to port 0 passed on an entry with no inbound grant")
	}
}

func TestPutScratchClearsTheBuffersItHeld(t *testing.T) {
	f := NewPacketFilter(nil)
	s := [][]byte{{1}, {2}}
	alias := s
	f.putScratch(&s)
	if alias[0] != nil || alias[1] != nil || len(s) != 0 {
		t.Error("the pooled slice still references wireguard-go's buffers")
	}
	f.putScratch(nil) // no scratch was taken: nothing to do
}

// Trailing bytes after the IP total length are ignored by the host, so a TCP
// header cannot hide there: the ports come from inside the total length.
func TestRestricted_PortsComeFromInsideTheTotalLength(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	p := fromPeer(circA, 40000, 22, fSYN)
	b := p.bytes()
	// 100 bytes of a second, "allowed" looking header after the real packet.
	extra := fromPeer(circA, 40000, 8340, fSYN).bytes()
	b = append(b, extra...)
	if f.verdict(b, dirIn) {
		t.Error("a SYN to 22 passed because an allowed-looking header trails it")
	}
}

// SYN flag combinations: only a SYN with no ACK, RST or FIN opens a flow.
func TestRestricted_OnlyAPureSynOpensAFlow(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	for name, fl := range map[string]byte{
		"syn+ack": fSYNACK, "syn+fin": tcpSYN | tcpFIN, "syn+rst": tcpSYN | tcpRST, "syn+fin+ack": tcpSYN | tcpFIN | tcpACK, "syn+rst+ack": tcpSYN | tcpRST | tcpACK,
	} {
		if f.in(fromPeer(circA, 40000, 8340, fl)) {
			t.Errorf("%s opened a flow", name)
		}
		if f.out(toPeer(circA, 50000, 9000, fl)) {
			t.Errorf("%s opened an outbound flow", name)
		}
	}
	// ECN setup SYN (ECE|CWR) and SYN with data (TCP Fast Open) are ordinary SYNs.
	if !f.in(fromPeer(circA, 40000, 8340, fSYN|0x40|0x80)) {
		t.Error("ECN-setup SYN was dropped")
	}
	p := fromPeer(circA, 40001, 8340, fSYN)
	p.payload = 100
	if !f.in(p) {
		t.Error("SYN carrying data was dropped")
	}
	if got := f.Entries()[0].Flows; got != 2 {
		t.Errorf("flows = %d, want 2", got)
	}
}

// ---------------------------------------------------------------- flow lifecycle

func TestFlow_FinThenClose(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 8340, 40000, fSYNACK))
	f.in(fromPeer(circA, 40000, 8340, fACK))

	// The peer finishes sending (half close). The host can still answer.
	if !f.in(fromPeer(circA, 40000, 8340, fFINACK)) {
		t.Fatal("FIN on a tracked flow was dropped")
	}
	clk.advance(2 * time.Minute)
	if !f.out(toPeer(circA, 8340, 40000, fPSHACK)) {
		t.Error("after the peer's FIN the host's data was dropped; a half-closed connection must still answer")
	}
	// The host finishes too: both sides have sent FIN.
	if !f.out(toPeer(circA, 8340, 40000, fFINACK)) {
		t.Fatal("our FIN was dropped")
	}
	// The last ACK and a FIN retransmission still go through inside the grace period.
	clk.advance(flowCloseGrace - time.Second)
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("the final ACK was dropped inside the close grace")
	}
	// After the grace the flow is gone.
	clk.advance(2 * time.Second)
	if f.in(fromPeer(circA, 40000, 8340, fACK)) || f.out(toPeer(circA, 8340, 40000, fACK)) {
		t.Error("a closed flow still passes packets after the grace period")
	}
	// The same ports can open a new connection.
	if !f.in(fromPeer(circA, 40000, 8340, fSYN)) {
		t.Error("a new SYN on the same ports was dropped")
	}
}

func TestFlow_RstTearsDownAtOnce(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 8340, 40000, fSYNACK))
	if !f.out(toPeer(circA, 8340, 40000, fRSTACK)) {
		t.Error("the RST itself must be delivered")
	}
	if f.in(fromPeer(circA, 40000, 8340, fACK)) || f.out(toPeer(circA, 8340, 40000, fPSHACK)) {
		t.Error("packets passed after the connection was reset")
	}
	// A RST for a flow that is not tracked is dropped, not a free pass.
	if f.in(fromPeer(circA, 40009, 8340, tcpRST)) {
		t.Error("RST of no flow passed")
	}
}

func TestFlow_IdleExpiryAndRefresh(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 2*time.Hour)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.in(fromPeer(circA, 40001, 8340, fSYN))

	clk.advance(flowIdleTimeout - time.Second)
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) { // refreshes flow 40000 only
		t.Fatal("flow dropped before the idle timeout")
	}
	clk.advance(2 * time.Second)
	if f.in(fromPeer(circA, 40001, 8340, fACK)) {
		t.Error("an idle flow passed after the idle timeout")
	}
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("a flow that saw traffic was dropped as idle")
	}
	// Idle in either direction counts: a packet from the host keeps it alive.
	clk.advance(flowIdleTimeout - time.Second)
	f.out(toPeer(circA, 8340, 40000, fPSHACK))
	clk.advance(flowIdleTimeout - time.Second)
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("host traffic did not refresh the flow")
	}
}

func TestFlow_Cap(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 4*time.Hour)
	mustEntry(t, f, circB, 8340, 0, 4*time.Hour)
	for i := 0; i < maxFlowsPerPeer; i++ {
		if !f.in(fromPeer(circA, uint16(1000+i), 8340, fSYN)) {
			t.Fatalf("SYN %d dropped below the cap", i)
		}
	}
	if f.in(fromPeer(circA, 5000, 8340, fSYN)) {
		t.Error("SYN over the cap passed")
	}
	if dropCount(f, "flow_cap") != 1 {
		t.Errorf("flow_cap drops = %d, want 1", dropCount(f, "flow_cap"))
	}
	// A SYN retransmission for an existing flow is not a new flow.
	if !f.in(fromPeer(circA, 1000, 8340, fSYN)) {
		t.Error("retransmitted SYN for a tracked flow dropped at the cap")
	}
	// The cap is per peer.
	if !f.in(fromPeer(circB, 1000, 8340, fSYN)) {
		t.Error("another peer was limited by this peer's flows")
	}
	// An over-cap peer's established flows keep working.
	if !f.in(fromPeer(circA, 1001, 8340, fACK)) {
		t.Error("established flow dropped at the cap")
	}
	// A reset frees a slot.
	f.in(fromPeer(circA, 1002, 8340, tcpRST))
	if !f.in(fromPeer(circA, 5000, 8340, fSYN)) {
		t.Error("a freed slot was not usable")
	}
	if f.in(fromPeer(circA, 5001, 8340, fSYN)) {
		t.Error("the table grew past the cap")
	}
	// Idle flows free their slots on the next SYN.
	clk.advance(flowIdleTimeout + time.Second)
	if !f.in(fromPeer(circA, 6000, 8340, fSYN)) {
		t.Error("idle flows were not swept to make room")
	}
	if got := f.Entries()[0].Flows; got != 1 {
		t.Errorf("flows after the sweep = %d, want 1", got)
	}
}

// ---------------------------------------------------------------- deadlines

// ticks moves both clocks forward by total in steps shorter than the idle
// timeout, sending an ACK on each flow after every step so the flows never go
// idle. It lets a test reach a deadline with its flows still live.
func ticks(clk *fakeClock, total time.Duration, touch func()) {
	const step = time.Minute
	for total > 0 {
		d := step
		if total < d {
			d = total
		}
		clk.advance(d)
		total -= d
		touch()
	}
}

func TestDeadline_MonotonicClock(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, 10*time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 50000, 9000, fSYN))
	touch := func() {
		f.in(fromPeer(circA, 40000, 8340, fACK))
		f.out(toPeer(circA, 50000, 9000, fACK))
	}
	ticks(clk, 10*time.Minute-time.Second, touch)
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) || !f.out(toPeer(circA, 50000, 9000, fACK)) {
		t.Fatal("grant dropped before its deadline")
	}
	clk.advance(2 * time.Second)
	for name, ok := range map[string]bool{
		"existing inbound flow":  f.in(fromPeer(circA, 40000, 8340, fACK)),
		"existing outbound flow": f.out(toPeer(circA, 50000, 9000, fPSHACK)),
		"reply on a flow":        f.in(fromPeer(circA, 9000, 50000, fSYNACK)),
		"new inbound SYN":        f.in(fromPeer(circA, 40003, 8340, fSYN)),
		"new outbound SYN":       f.out(toPeer(circA, 50003, 9000, fSYN)),
		"a RST":                  f.in(fromPeer(circA, 40000, 8340, tcpRST)),
	} {
		if ok {
			t.Errorf("%s passed after the deadline", name)
		}
	}
	if dropCount(f, "entry_expired") != 6 {
		t.Errorf("entry_expired drops = %d, want 6", dropCount(f, "entry_expired"))
	}
	if e := f.Entries()[0]; !e.Expired || e.Remaining > 0 {
		t.Errorf("Entries() = %+v, want expired", e)
	}
}

// A machine that sleeps: the monotonic clock stops, the wall clock does not.
func TestDeadline_WallClockCatchesSleep(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 10*time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	clk.sleep(time.Hour) // wall +1h, mono +0
	if f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("an existing flow survived an hour of sleep on a 10 minute grant")
	}
	if f.in(fromPeer(circA, 40001, 8340, fSYN)) {
		t.Error("a new flow was admitted after an hour of sleep on a 10 minute grant")
	}
}

// The wall clock set backwards must not extend a grant: the monotonic clock
// still runs it out.
func TestDeadline_WallClockSetBackDoesNotExtend(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 10*time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	touch := func() { f.in(fromPeer(circA, 40000, 8340, fACK)) }
	clk.setBack(24 * time.Hour)
	ticks(clk, 5*time.Minute, touch) // wall is now a day behind, mono 5 minutes in
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("grant dropped early by a backwards wall clock")
	}
	ticks(clk, 6*time.Minute, touch) // mono 11 minutes in
	if f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("a backwards wall clock kept a grant alive past its monotonic deadline")
	}
}

func TestDeadline_ReportedRemainingIsTheSmallerClock(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 10*time.Minute)
	if got := f.Entries()[0].Remaining; got != 10*time.Minute {
		t.Errorf("remaining = %v, want 10m", got)
	}
	clk.sleep(4 * time.Minute)
	if got := f.Entries()[0].Remaining; got != 6*time.Minute {
		t.Errorf("remaining after a 4 minute wall jump = %v, want 6m", got)
	}
	clk.advance(2 * time.Minute) // mono 2m, wall 6m in
	if got := f.Entries()[0].Remaining; got != 4*time.Minute {
		t.Errorf("remaining = %v, want 4m", got)
	}
}

// ---------------------------------------------------------------- the entry API

func TestSetEntry_ReplaceExtendsAndKeepsFlowsOnSamePorts(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, 10*time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 50000, 9000, fSYN))
	clk.advance(4 * time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fACK)) // traffic keeps the flows from going idle
	f.out(toPeer(circA, 50000, 9000, fACK))
	clk.advance(4 * time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fACK))
	f.out(toPeer(circA, 50000, 9000, fACK))
	mustEntry(t, f, circA, 8340, 9000, 10*time.Minute) // renewal, 8 minutes in
	clk.advance(4 * time.Minute)                       // 12 minutes in: past the first deadline, inside the second
	if !f.in(fromPeer(circA, 40000, 8340, fACK)) || !f.out(toPeer(circA, 50000, 9000, fACK)) {
		t.Error("a renewal cut flows that were in flight")
	}
	if len(f.Entries()) != 1 {
		t.Errorf("entries = %d, want 1", len(f.Entries()))
	}
}

// A grant that is renewed after it ran out starts clean: connections opened
// under the old one do not come back.
func TestSetEntry_RenewalAfterExpiryDoesNotRevivePriorFlows(t *testing.T) {
	f, clk := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 0, 2*time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	clk.advance(3 * time.Minute) // expired; the flow is still inside its 5 minute idle window
	if f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Fatal("expired grant passed a packet")
	}
	mustEntry(t, f, circA, 8340, 0, time.Hour)
	if f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("a flow from the expired grant came back after the entry was set again")
	}
	if !f.in(fromPeer(circA, 40000, 8340, fSYN)) || !f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("a fresh connection did not work after the renewal")
	}
}

func TestSetEntry_ReplaceWithOtherPortsForgetsThoseFlows(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.out(toPeer(circA, 50000, 9000, fSYN))
	mustEntry(t, f, circA, 8350, 9000, time.Hour) // inbound port moved
	if f.in(fromPeer(circA, 40000, 8340, fACK)) {
		t.Error("a flow opened under the old inbound port still passes")
	}
	if !f.out(toPeer(circA, 50000, 9000, fACK)) {
		t.Error("the outbound flow, whose port did not change, was cut")
	}
	mustEntry(t, f, circA, 8350, 0, time.Hour) // outbound grant withdrawn
	if f.out(toPeer(circA, 50000, 9000, fACK)) || f.out(toPeer(circA, 50001, 9000, fSYN)) {
		t.Error("outbound traffic passes after the outbound grant was withdrawn")
	}
	if !f.in(fromPeer(circA, 40001, 8350, fSYN)) {
		t.Error("the new inbound port does not work")
	}
}

func TestRemoveEntry_StopsEverythingAtOnce(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	mustEntry(t, f, circA, 8340, 9000, time.Hour)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	e := f.table.Load().m[circA]
	f.RemoveEntry(addr(circA))
	f.RemoveEntry(addr(circA)) // idempotent
	f.RemoveEntry(addr(strngr))
	f.RemoveEntry(netip.MustParseAddr("2001:db8::1"))
	if f.in(fromPeer(circA, 40000, 8340, fACK)) || f.in(fromPeer(circA, 40001, 8340, fSYN)) || f.out(toPeer(circA, 1, 9000, fSYN)) {
		t.Error("a removed peer still passes packets")
	}
	if dropCount(f, "not_a_peer") != 3 {
		t.Errorf("not_a_peer = %d, want 3", dropCount(f, "not_a_peer"))
	}
	// A packet that had already found the entry before it was removed is refused.
	if r := e.admit(dirIn, 40000, 8340, fACK, 0, 0); r != dropGone {
		t.Errorf("admit on a removed entry = %v, want %v", r, dropGone)
	}
	if len(f.Entries()) != 0 {
		t.Errorf("entries = %v, want none", f.Entries())
	}
}

func TestSetEntry_Validation(t *testing.T) {
	f, _ := newTestFilter(t)
	g := Grant{InPort: 8340, TTL: time.Minute}
	for name, a := range map[string]netip.Addr{
		"ipv6":        netip.MustParseAddr("2001:db8::1"),
		"zero value":  {},
		"unspecified": netip.MustParseAddr("0.0.0.0"),
		"loopback":    netip.MustParseAddr("127.0.0.1"),
		"multicast":   netip.MustParseAddr("224.0.0.251"),
		"this host":   addr(selfA),
	} {
		if err := f.SetEntry(a, g); err != ErrBadPeer {
			t.Errorf("%s: err = %v, want ErrBadPeer", name, err)
		}
	}
	if err := f.SetEntry(addr(circA), Grant{InPort: 1, TTL: 0}); err != ErrBadTTL {
		t.Errorf("zero ttl: err = %v", err)
	}
	if err := f.SetEntry(addr(circA), Grant{InPort: 1, TTL: -time.Second}); err != ErrBadTTL {
		t.Errorf("negative ttl: err = %v", err)
	}
	if err := f.SetEntry(addr(circA), Grant{InPort: 1, TTL: maxGrantTTL + time.Second}); err != ErrBadTTL {
		t.Errorf("ttl over the cap: err = %v", err)
	}
	if err := f.SetEntry(addr(circA), Grant{InPort: 1, TTL: time.Duration(1<<63 - 1)}); err != ErrBadTTL {
		t.Errorf("huge ttl: err = %v", err)
	}
	if len(f.Entries()) != 0 {
		t.Errorf("a refused SetEntry left an entry behind: %v", f.Entries())
	}
	// an IPv4-mapped IPv6 spelling of a valid address is accepted as that address
	if err := f.SetEntry(netip.MustParseAddr("::ffff:100.64.1.7"), g); err != nil {
		t.Errorf("mapped address: %v", err)
	}
	if len(f.Entries()) != 1 || f.Entries()[0].Peer != addr(circA) {
		t.Errorf("entries = %v", f.Entries())
	}
	for i := 0; i < maxFilterEntries-1; i++ {
		if err := f.SetEntry(addr([4]byte{100, 64, 2, byte(i)}), g); err != nil {
			t.Fatalf("entry %d: %v", i, err)
		}
	}
	if err := f.SetEntry(addr([4]byte{100, 64, 3, 1}), g); err != ErrTooManyEntries {
		t.Errorf("past the cap: err = %v, want ErrTooManyEntries", err)
	}
	// replacing an existing entry is not growth
	if err := f.SetEntry(addr(circA), g); err != nil {
		t.Errorf("replace at the cap: %v", err)
	}
	// Setting an entry for the host's own address is refused, but the host
	// address is not known to a filter that was never told it.
	g2 := NewPacketFilter(nil)
	if err := g2.SetEntry(addr(selfA), g); err != nil {
		t.Errorf("with no self address yet: %v", err)
	}
}

func TestEntries_ListsOrderedWithFlowCounts(t *testing.T) {
	f, _ := newTestFilter(t)
	mustEntry(t, f, circB, 0, 9000, time.Hour)
	mustEntry(t, f, circA, 8340, 0, time.Minute)
	f.in(fromPeer(circA, 40000, 8340, fSYN))
	f.in(fromPeer(circA, 40001, 8340, fSYN))
	got := f.Entries()
	if len(got) != 2 || got[0].Peer != addr(circA) || got[1].Peer != addr(circB) {
		t.Fatalf("entries = %+v", got)
	}
	if got[0].InPort != 8340 || got[0].OutPort != 0 || got[0].Flows != 2 || got[0].Expired {
		t.Errorf("entry A = %+v", got[0])
	}
	if got[1].InPort != 0 || got[1].OutPort != 9000 || got[1].Flows != 0 {
		t.Errorf("entry B = %+v", got[1])
	}
}

// ---------------------------------------------------------------- panic

func TestPanicInHouseholdLookupDropsThePacket(t *testing.T) {
	f := NewPacketFilter(func(a [4]byte) bool {
		if a == hhB {
			panic("boom")
		}
		return a == hhA
	})
	f.SetSelf(addr(selfA).AsSlice())
	if f.in(pkt{src: hhB, dst: selfA}) {
		t.Error("a packet that made the filter panic passed")
	}
	if !f.in(pkt{src: hhA, dst: selfA}) {
		t.Error("the filter stopped working after a panic")
	}
	if dropCount(f, "panic") != 1 {
		t.Errorf("panic drops = %d, want 1", dropCount(f, "panic"))
	}
}

// A stream of packets that make the filter panic, or that come from an unknown
// mesh address, logs once per interval, not once per packet.
func TestLogLinesAreSpaced(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	defer slog.SetDefault(prev)

	f, clk := newTestFilter(t)
	for i := 0; i < 1000; i++ {
		f.in(pkt{src: strngr, dst: selfA, flags: fSYN})
	}
	if n := strings.Count(buf.String(), "not a household peer"); n != 1 {
		t.Errorf("1000 packets from an unknown mesh address logged %d lines, want 1", n)
	}
	clk.advance(warnInterval + time.Second)
	f.in(pkt{src: strngr, dst: selfA, flags: fSYN})
	if n := strings.Count(buf.String(), "not a household peer"); n != 2 {
		t.Errorf("after the interval the line was logged %d times in total, want 2", n)
	}
	// Addresses outside the mesh range (multicast chatter) never log.
	buf.Reset()
	for i := 0; i < 100; i++ {
		f.out(pkt{src: selfA, dst: [4]byte{224, 0, 0, 251}, proto: 17})
	}
	if buf.Len() != 0 {
		t.Errorf("multicast drops logged: %s", buf.String())
	}

	buf.Reset()
	boom := NewPacketFilter(func([4]byte) bool { panic("boom") })
	boom.now = clk.read
	for i := 0; i < 1000; i++ {
		boom.in(pkt{src: hhA, dst: selfA})
	}
	if n := strings.Count(buf.String(), "panic while judging"); n != 1 {
		t.Errorf("1000 panics logged %d lines, want 1", n)
	}
	if got := dropCount(boom, "panic"); got != 1000 {
		t.Errorf("panic drops = %d, want 1000", got)
	}
}

func TestPanicInTableDropsThePacket(t *testing.T) {
	f, _ := newTestFilter(t, hhA)
	f.table.Store(nil) // a broken filter must fail closed
	if f.in(pkt{src: hhA, dst: selfA}) {
		t.Error("a packet passed through a broken filter")
	}
}

// ---------------------------------------------------------------- no test hooks in the shipped filter

func TestSelfDescribes(t *testing.T) {
	// Every drop reason has a name, because Stats() is built from them.
	for r := dropReason(0); r < numDropReasons; r++ {
		if r.String() == "" {
			t.Errorf("drop reason %d has no name", r)
		}
	}
	_ = fmt.Sprint(Grant{})
}

// SetMeshIP is how the filter learns this host's address. The interface does
// not exist in this test, so assigning the address fails (and, on Windows where
// it would go on to touch the firewall, the test does not run); the filter has
// been told by then, because it must know before the interface carries the address.
func TestSetMeshIPTellsTheFilter(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("SetMeshIP configures the Windows firewall after assigning the address")
	}
	f := NewPacketFilter(nil)
	tn := &Tunnel{ifName: "meshtest-nonexistent0", wgPort: 51820, filter: f}
	if err := tn.SetMeshIP("not an address"); err == nil {
		t.Fatal("an invalid address was accepted")
	}
	if f.self.Load() != 0 {
		t.Error("an invalid address reached the filter")
	}
	_ = tn.SetMeshIP("100.64.0.9") // fails to assign on a missing interface
	if got := f.selfAddr(); got != [4]byte{100, 64, 0, 9} {
		t.Errorf("filter self address = %v, want 100.64.0.9", got)
	}
	// A tunnel built without a packet policy has no filter to tell.
	_ = (&Tunnel{ifName: "meshtest-nonexistent0", wgPort: 51820}).SetMeshIP("100.64.0.9")
}
