package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/crypto/curve25519"
	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/tuntest"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// This file runs the start of a direct-path trial between two real wireguard-go
// devices in one process (loopback UDP, a channel TUN each), driven by the real
// rosterHandler.OnPunchCoord on both sides at the same instant, which is what the
// coordinator's punch_coord does. It checks what trialverify.go claims: with one
// machine starting the handshake, packets flow in both directions soon after, every
// time. The same runs with both machines starting the handshake (what a trial did
// before) are in TestTrialCrossingDemo, off unless POTLUCK_CROSSING_DEMO=1 because
// they are expected to fail part of the time.

type wgTestNode struct {
	name   string
	priv   [32]byte
	pub    [32]byte
	dev    *device.Device
	ch     *tuntest.ChannelTUN
	port   int
	meshIP netip.Addr
	rx     chan netip.Addr // source address of every IP packet the device delivered to its TUN

	// What the machine around the device does (used by trial_oneway_test.go). Off by default, so
	// the tests in this file see a machine that only carries packets.
	answers     atomic.Bool     // the system answers an echo request (stands for a closed port answering a SYN)
	deaf        atomic.Bool     // drops every inbound datagram that is not a handshake message: a session whose inbound is dead
	healOnClear atomic.Bool     // the peer being removed and added again repairs deaf, as it repairs the keypair race's victim
	removes     atomic.Int32    // RemovePeer calls
	replies     chan netip.Addr // echo replies that arrived
	watchMu     sync.Mutex      // guards watched
	watched     map[netip.Addr]*atomic.Uint64
}

// deafBind drops inbound transport datagrams while deaf is set. Handshake messages still pass.
type deafBind struct {
	conn.Bind
	deaf *atomic.Bool
}

func (b deafBind) Open(port uint16) ([]conn.ReceiveFunc, uint16, error) {
	fns, actual, err := b.Bind.Open(port)
	for i, fn := range fns {
		fn := fn
		fns[i] = func(packets [][]byte, sizes []int, eps []conn.Endpoint) (int, error) {
			n, err := fn(packets, sizes, eps)
			if b.deaf.Load() {
				for j := 0; j < n; j++ {
					if sizes[j] >= 4 && binary.LittleEndian.Uint32(packets[j][:4]) == 4 { // message type 4: transport data
						sizes[j] = 0
					}
				}
			}
			return n, err
		}
	}
	return fns, actual, err
}

func newWGTestNode(t *testing.T, name string, meshIP string) *wgTestNode {
	t.Helper()
	n := &wgTestNode{name: name, meshIP: netip.MustParseAddr(meshIP), rx: make(chan netip.Addr, 256),
		replies: make(chan netip.Addr, 256), watched: map[netip.Addr]*atomic.Uint64{}}
	if _, err := rand.Read(n.priv[:]); err != nil {
		t.Fatal(err)
	}
	n.priv[0] &= 248
	n.priv[31] = (n.priv[31] & 127) | 64
	pub, err := curve25519.X25519(n.priv[:], curve25519.Basepoint)
	if err != nil {
		t.Fatal(err)
	}
	copy(n.pub[:], pub)
	n.ch = tuntest.NewChannelTUN()
	level := device.LogLevelSilent
	if os.Getenv("POTLUCK_CROSSING_LOG") != "" {
		level = device.LogLevelVerbose
	}
	n.dev = device.NewDevice(n.ch.TUN(), deafBind{Bind: conn.NewDefaultBind(), deaf: &n.deaf}, device.NewLogger(level, name+" "))
	if err := n.dev.IpcSet(fmt.Sprintf("private_key=%x\nlisten_port=0\n", n.priv)); err != nil {
		n.dev.Close()
		t.Skipf("cannot open a UDP socket for a wireguard-go device here: %v", err)
	}
	if err := n.dev.Up(); err != nil {
		t.Skipf("cannot bring up a wireguard-go device here: %v", err)
	}
	text, err := n.dev.IpcGet()
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range strings.Split(text, "\n") {
		if v, ok := strings.CutPrefix(line, "listen_port="); ok {
			n.port, _ = strconv.Atoi(v)
		}
	}
	if n.port == 0 {
		t.Fatal("device reports no listen port")
	}
	go func() {
		for pkt := range n.ch.Inbound {
			if len(pkt) < 20 {
				continue
			}
			src := netip.AddrFrom4([4]byte(pkt[12:16]))
			n.watchMu.Lock()
			if c, ok := n.watched[src]; ok {
				c.Add(1) // what the packet filter's inbound watch does
			}
			n.watchMu.Unlock()
			if pkt[9] == 1 && len(pkt) >= 21 { // ICMP: the machine's own stack
				switch pkt[20] {
				case 8: // echo request
					if n.answers.Load() {
						reply := append([]byte(nil), pkt...)
						copy(reply[12:16], pkt[16:20])
						copy(reply[16:20], pkt[12:16])
						reply[20] = 0
						select {
						case n.ch.Outbound <- reply:
						case <-time.After(time.Second):
						}
					}
				case 0: // echo reply
					select {
					case n.replies <- src:
					default:
					}
				}
			}
			select {
			case n.rx <- src:
			default:
			}
		}
	}()
	t.Cleanup(func() { n.dev.Close() })
	return n
}

func (n *wgTestNode) pubB64() string { return base64.StdEncoding.EncodeToString(n.pub[:]) }

// send makes the kernel hand this node's device an IP packet for dst.
func (n *wgTestNode) send(dst netip.Addr) {
	select {
	case n.ch.Outbound <- tuntest.Ping(dst, n.meshIP):
	case <-time.After(time.Second):
	}
}

// wgTunnel is the rosterTunnel the handler uses, over a raw device.
type wgTunnel struct{ n *wgTestNode }

func uapiPeer(p tunnel.Peer) string {
	kb, _ := base64.StdEncoding.DecodeString(p.PublicKey)
	var sb strings.Builder
	fmt.Fprintf(&sb, "public_key=%s\n", hex.EncodeToString(kb))
	if p.Endpoint != "" {
		fmt.Fprintf(&sb, "endpoint=%s\n", p.Endpoint)
	}
	if len(p.AllowedIPs) > 0 {
		sb.WriteString("replace_allowed_ips=true\n")
	}
	for _, c := range p.AllowedIPs {
		fmt.Fprintf(&sb, "allowed_ip=%s\n", c)
	}
	if p.PersistentKeepalive > 0 {
		fmt.Fprintf(&sb, "persistent_keepalive_interval=%d\n", p.PersistentKeepalive)
	}
	return sb.String()
}

func (w *wgTunnel) SetMeshIP(string) error       { return nil }
func (w *wgTunnel) SetPeers([]tunnel.Peer) error { return nil }
func (w *wgTunnel) AddPeer(p tunnel.Peer) error  { return w.n.dev.IpcSet(uapiPeer(p)) }
func (w *wgTunnel) AddPeerRoute(string) error    { return nil }
func (w *wgTunnel) RemovePeerRoute(string) error { return nil }
func (w *wgTunnel) UpdatePeerEndpoint(pub, ep string) error {
	kb, _ := base64.StdEncoding.DecodeString(pub)
	return w.n.dev.IpcSet(fmt.Sprintf("public_key=%s\nendpoint=%s\n", hex.EncodeToString(kb), ep))
}
func (w *wgTunnel) RemovePeer(pub string) error {
	w.n.removes.Add(1)
	if w.n.healOnClear.Load() {
		w.n.deaf.Store(false)
	}
	kb, _ := base64.StdEncoding.DecodeString(pub)
	return w.n.dev.IpcSet(fmt.Sprintf("public_key=%s\nremove=true\n", hex.EncodeToString(kb)))
}
func (w *wgTunnel) PeerReachability(pub string) (time.Time, uint64, uint64, error) {
	kb, _ := base64.StdEncoding.DecodeString(pub)
	want := hex.EncodeToString(kb)
	text, err := w.n.dev.IpcGet()
	if err != nil {
		return time.Time{}, 0, 0, err
	}
	var in bool
	var sec, nsec int64
	for _, line := range strings.Split(text, "\n") {
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		switch k {
		case "public_key":
			in = v == want
		case "last_handshake_time_sec":
			if in {
				sec, _ = strconv.ParseInt(v, 10, 64)
			}
		case "last_handshake_time_nsec":
			if in {
				nsec, _ = strconv.ParseInt(v, 10, 64)
			}
		}
	}
	if sec == 0 {
		return time.Time{}, 0, 0, nil
	}
	return time.Unix(sec, nsec), 0, 0, nil
}

// trialHandler builds a rosterHandler for node self that knows node other.
func trialHandler(t *testing.T, self, other *wgTestNode, selfKey string, followerDelay time.Duration) *rosterHandler {
	t.Helper()
	ep := fmt.Sprintf("127.0.0.1:%d", other.port)
	h := newRosterHandler(&wgTunnel{self}, peerstate.New(), nil, nil)
	h.ps.Upsert(msignal.RosterPeer{NodeID: "peer-" + other.name, WGPublicKey: other.pubB64(), MeshIP: other.meshIP.String(), LastEndpoint: &ep})
	h.localIPNets = func() []net.IPNet { return nil }
	h.modes = newModeTracker()
	h.punchTrials = newPunchTrialTracker()
	h.dBind = derp.NewDerpBind(nil, nil)
	h.dBind.SetPeerRouting(other.pub, true)
	h.selfKeyFn = func() string { return selfKey }
	h.followerDelay = followerDelay
	return h
}

// session sets the two devices up the way a pair is just before a trial: each knows the
// other's endpoint and a first exchange has made a session (the one the relay carried).
// One side starts it; two machines that both start a handshake at the same moment is
// the very thing under test.
func session(t *testing.T, a, b *wgTestNode) {
	t.Helper()
	cfg := func(from, to *wgTestNode, keepalive int) {
		if err := (&wgTunnel{from}).AddPeer(tunnel.Peer{
			PublicKey:           to.pubB64(),
			AllowedIPs:          []string{to.meshIP.String() + "/32"},
			Endpoint:            fmt.Sprintf("127.0.0.1:%d", to.port),
			PersistentKeepalive: keepalive,
		}); err != nil {
			t.Fatal(err)
		}
	}
	cfg(b, a, 0)
	cfg(a, b, 25)
	if _, ok := waitFlow(a, b, 3*time.Second); !ok {
		t.Fatal("the first exchange A to B did not get through")
	}
	if _, ok := waitFlow(b, a, 3*time.Second); !ok {
		t.Fatal("the first exchange B to A did not get through")
	}
	cfg(b, a, 25)
}

// waitFlow sends a packet from -> to every 100 ms until one arrives or the limit passes,
// and returns how long that took.
func waitFlow(from, to *wgTestNode, limit time.Duration) (time.Duration, bool) {
	for len(to.rx) > 0 { // packets from earlier attempts
		<-to.rx
	}
	start := time.Now()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	from.send(to.meshIP)
	for {
		select {
		case src := <-to.rx:
			if src == from.meshIP {
				return time.Since(start), true
			}
		case <-tick.C:
			if time.Since(start) > limit {
				return time.Since(start), false
			}
			from.send(to.meshIP)
		}
	}
}

type trialRun struct {
	ab, ba     time.Duration
	abOK, baOK bool
}

// startTrial makes both machines get their punch_coord at the same moment (skew apart if
// asked) and then times first delivery in each direction.
func startTrial(t *testing.T, a, b *wgTestNode, ha, hb *rosterHandler, skew time.Duration) trialRun {
	t.Helper()
	epA := fmt.Sprintf("127.0.0.1:%d", a.port)
	epB := fmt.Sprintf("127.0.0.1:%d", b.port)
	pcForA := msignal.PunchCoord{PeerNodeID: "peer-" + b.name, PeerWGPublicKey: b.pubB64(), PeerEndpoint: &epB, Role: "initiator"}
	pcForB := msignal.PunchCoord{PeerNodeID: "peer-" + a.name, PeerWGPublicKey: a.pubB64(), PeerEndpoint: &epA, Role: "responder"}
	var wg sync.WaitGroup
	gate := make(chan struct{})
	run := func(h *rosterHandler, pc msignal.PunchCoord, delay time.Duration) {
		defer wg.Done()
		<-gate
		time.Sleep(delay)
		h.OnPunchCoord(pc)
	}
	wg.Add(2)
	go run(ha, pcForA, 0)
	go run(hb, pcForB, skew)
	close(gate)
	t0 := time.Now()
	wg.Wait()

	var r trialRun
	var wg2 sync.WaitGroup
	wg2.Add(2)
	go func() { defer wg2.Done(); _, r.abOK = waitFlow(a, b, 4*time.Second); r.ab = time.Since(t0) }()
	go func() { defer wg2.Done(); _, r.baOK = waitFlow(b, a, 4*time.Second); r.ba = time.Since(t0) }()
	wg2.Wait()
	return r
}

func crossingRuns(def int) int {
	if v, err := strconv.Atoi(os.Getenv("POTLUCK_CROSSING_RUNS")); err == nil && v > 0 {
		return v
	}
	if testing.Short() {
		return def / 3
	}
	return def
}

// With one machine starting the handshake, traffic flows both ways within about two
// seconds of both punch_coords, on every run, with the two arriving together or a few
// milliseconds apart.
func TestTrialStart_BothDirectionsFlowEveryTime(t *testing.T) {
	const followerDelay = 60 * time.Millisecond
	runs := crossingRuns(36)
	skews := []time.Duration{0, 0, time.Millisecond, 3 * time.Millisecond}
	var worst time.Duration
	for i := 0; i < runs; i++ {
		a := newWGTestNode(t, "a", "100.64.0.1")
		b := newWGTestNode(t, "b", "100.64.0.2")
		session(t, a, b)
		ha := trialHandler(t, a, b, a.pubB64(), followerDelay)
		hb := trialHandler(t, b, a, b.pubB64(), followerDelay)
		skew := skews[i%len(skews)]
		r := startTrial(t, a, b, ha, hb, skew)
		if !r.abOK || !r.baOK {
			t.Fatalf("run %d (skew %v): a to b ok=%v after %v, b to a ok=%v after %v", i, skew, r.abOK, r.ab, r.baOK, r.ba)
		}
		for _, d := range []time.Duration{r.ab, r.ba} {
			if d > worst {
				worst = d
			}
			if d > 2*time.Second {
				t.Fatalf("run %d (skew %v): first packet took %v, want about 2 s at most", i, skew, d)
			}
		}
		time.Sleep(followerDelay + 30*time.Millisecond) // let the follower's timer fire before the devices close
		a.dev.Close()
		b.dev.Close()
	}
	t.Logf("%d runs, every direction delivered; slowest first delivery %v", runs, worst)
}

// The behaviour before the change: both machines start the handshake. Not run by default.
// POTLUCK_CROSSING_DEMO=1 go test -run TestTrialCrossingDemo -v ./cmd/potluck-mesh
func TestTrialCrossingDemo(t *testing.T) {
	if os.Getenv("POTLUCK_CROSSING_DEMO") == "" {
		t.Skip("set POTLUCK_CROSSING_DEMO=1 to run the pre-change behaviour (both machines start the handshake); part of the runs are expected to break")
	}
	runs := crossingRuns(60)
	var okBoth, oneWay, none int
	for i := 0; i < runs; i++ {
		a := newWGTestNode(t, "a", "100.64.0.1")
		b := newWGTestNode(t, "b", "100.64.0.2")
		session(t, a, b)
		// An empty own key reads as "no key known": this machine starts the handshake, which is
		// what every trial did before.
		ha := trialHandler(t, a, b, "", time.Hour)
		hb := trialHandler(t, b, a, "", time.Hour)
		r := startTrial(t, a, b, ha, hb, 0)
		switch {
		case r.abOK && r.baOK:
			okBoth++
		case r.abOK || r.baOK:
			oneWay++
		default:
			none++
		}
		a.dev.Close()
		b.dev.Close()
	}
	t.Logf("both machines start the handshake, %d runs: both directions %d, one direction only %d, neither %d (each judged after 4 s)", runs, okBoth, oneWay, none)
}
