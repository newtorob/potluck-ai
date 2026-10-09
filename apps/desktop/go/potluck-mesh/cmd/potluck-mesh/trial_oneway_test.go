package main

import (
	"encoding/base64"
	"fmt"
	"log/slog"
	"net/netip"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// Scenarios for the verification of a direct-path trial between two real wireguard-go devices in
// one process. Both handlers run the real OnPunchCoord, and both machines run the real
// punchTrialTick, with the time moved two seconds per tick. What the real thing has and these
// have no way to run is the operating system, so each node stands for it:
//
//   - answers: the system answers a probe (an echo request here, a SYN to a closed port in the
//     sidecar). Off means a system in stealth mode: it answers nothing.
//   - deaf: the node's inbound transport datagrams are dropped while handshake messages pass, a
//     session that works one way only. With healOnClear the fault goes when the peer is removed and
//     added again, as the keypair race's victim does; without it the fault stays.
//
// The inbound watch is a counter of the packets that came out of the node's TUN from the peer, which
// is what the packet filter's watch is.

type nodeWatch struct{ n *wgTestNode }

func (w nodeWatch) WatchInbound(a netip.Addr) {
	w.n.watchMu.Lock()
	defer w.n.watchMu.Unlock()
	if _, ok := w.n.watched[a]; !ok {
		w.n.watched[a] = new(atomic.Uint64)
	}
}
func (w nodeWatch) UnwatchInbound(a netip.Addr) {
	w.n.watchMu.Lock()
	defer w.n.watchMu.Unlock()
	delete(w.n.watched, a)
}
func (w nodeWatch) InboundCount(a netip.Addr) uint64 {
	w.n.watchMu.Lock()
	defer w.n.watchMu.Unlock()
	if c, ok := w.n.watched[a]; ok {
		return c.Load()
	}
	return 0
}

// probeVia is the probe of node n toward peer: one echo request through the tunnel, and whether
// the peer's system answered it within a moment.
func probeVia(n, peer *wgTestNode) func(local, peerIP string, d time.Duration) bool {
	return func(string, string, time.Duration) bool {
		for len(n.replies) > 0 {
			<-n.replies
		}
		n.send(peer.meshIP)
		select {
		case src := <-n.replies:
			return src == peer.meshIP
		case <-time.After(100 * time.Millisecond):
			return false
		}
	}
}

type trialLogCapture struct {
	mu sync.Mutex
	b  strings.Builder
}

func (c *trialLogCapture) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.b.Write(p)
}
func (c *trialLogCapture) lines(contains ...string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
outer:
	for _, l := range strings.Split(c.b.String(), "\n") {
		for _, s := range contains {
			if !strings.Contains(l, s) {
				continue outer
			}
		}
		n++
	}
	return n
}

func captureTrialLog(t *testing.T) *trialLogCapture {
	t.Helper()
	c := &trialLogCapture{}
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(c, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(old) })
	return c
}

func keyOf(b byte) string {
	k := [32]byte{}
	for i := range k {
		k[i] = b
	}
	return base64.StdEncoding.EncodeToString(k[:])
}

type oneWayCase struct {
	name               string
	leader             string // "a" or "b": the machine that starts the handshake
	aAnswers, bAnswers bool
	victim             string // "", "a" or "b": the machine whose inbound transport is dead
	heal               bool   // the fault goes when the victim adds the peer again
	faultFromStart     bool   // the fault is there before the handshake completes; otherwise it starts once the handshake has completed
	wantA, wantB       routingMode
	rekeyA, rekeyB     bool // the machine added the peer again
	noPathA, noPathB   bool // the machine ended its trial with NO DATA PATH
	byA, byB           string
}

func runOneWayCase(t *testing.T, c oneWayCase) {
	logs := captureTrialLog(t)
	a := newWGTestNode(t, "a", "100.64.0.1")
	b := newWGTestNode(t, "b", "100.64.0.2")
	a.answers.Store(c.aAnswers)
	b.answers.Store(c.bAnswers)
	session(t, a, b)
	// A key lower than any real one makes a machine the leader, a higher one the follower.
	keyA, keyB := keyOf(0xff), keyOf(0x00)
	if c.leader == "a" {
		keyA, keyB = keyOf(0x00), keyOf(0xff)
	}
	ha := trialHandler(t, a, b, keyA, 60*time.Millisecond)
	hb := trialHandler(t, b, a, keyB, 60*time.Millisecond)
	for _, x := range []struct {
		h       *rosterHandler
		n, peer *wgTestNode
	}{{ha, a, b}, {hb, b, a}} {
		x.h.verifier = newTrialVerifier(func() string { return x.n.meshIP.String() }, nodeWatch{x.n})
		x.h.verifier.probe = probeVia(x.n, x.peer)
	}

	epA, epB := fmt.Sprintf("127.0.0.1:%d", a.port), fmt.Sprintf("127.0.0.1:%d", b.port)
	var wg sync.WaitGroup
	gate := make(chan struct{})
	for _, x := range []struct {
		h  *rosterHandler
		pc msignal.PunchCoord
	}{
		{ha, msignal.PunchCoord{PeerNodeID: "peer-b", PeerWGPublicKey: b.pubB64(), PeerEndpoint: &epB, Role: "initiator"}},
		{hb, msignal.PunchCoord{PeerNodeID: "peer-a", PeerWGPublicKey: a.pubB64(), PeerEndpoint: &epA, Role: "responder"}},
	} {
		wg.Add(1)
		go func() { defer wg.Done(); <-gate; x.h.OnPunchCoord(x.pc) }()
	}
	close(gate)
	wg.Wait()

	// The fault starts once the trial has been started on both machines and, unless asked
	// otherwise, once the handshake has completed on both (the keypair race's victim has
	// completed it; it is the session that is broken).
	if !c.faultFromStart {
		time.Sleep(25 * time.Millisecond)
	}
	switch c.victim {
	case "a":
		a.healOnClear.Store(c.heal)
		a.deaf.Store(true)
	case "b":
		b.healOnClear.Store(c.heal)
		b.deaf.Store(true)
	}

	tickA := punchTrialTick(ha.tun, ha.ps, ha.dBind, nil, ha.modes, ha.punchTrials, ha.verifier)
	tickB := punchTrialTick(hb.tun, hb.ps, hb.dBind, nil, hb.modes, hb.punchTrials, hb.verifier)
	over := func() bool {
		return ha.modes.get(b.pubB64()) != modePunchTrial && hb.modes.get(a.pubB64()) != modePunchTrial
	}
	now := time.Now()
	ticks := 0
	for ticks < int(punchTrialDuration/punchVerifyInterval) && !over() {
		now = now.Add(punchVerifyInterval)
		tickA(now)
		tickB(now)
		ticks++
		time.Sleep(200 * time.Millisecond) // real time for the packets, the probes and the handshakes
	}
	time.Sleep(100 * time.Millisecond)

	if got := ha.modes.get(b.pubB64()); got != c.wantA {
		t.Errorf("a's trial ended in %v, want %v", got, c.wantA)
	}
	if got := hb.modes.get(a.pubB64()); got != c.wantB {
		t.Errorf("b's trial ended in %v, want %v", got, c.wantB)
	}
	pa, pb := "pubkey_prefix="+util.TruncatePubkey(b.pubB64()), "pubkey_prefix="+util.TruncatePubkey(a.pubB64())
	for _, x := range []struct {
		who, prefix   string
		rekey, nopath bool
		by            string
	}{{"a", pa, c.rekeyA, c.noPathA, c.byA}, {"b", pb, c.rekeyB, c.noPathB, c.byB}} {
		if got := logs.lines("adding the peer again once", x.prefix) > 0; got != x.rekey {
			t.Errorf("%s added the peer again = %v, want %v", x.who, got, x.rekey)
		}
		if got := logs.lines("NO DATA PATH", x.prefix) > 0; got != x.nopath {
			t.Errorf("%s ended with NO DATA PATH = %v, want %v", x.who, got, x.nopath)
		}
		if x.by != "" && logs.lines("punch_trial: VERIFIED", x.prefix, "verified_by="+x.by) == 0 {
			t.Errorf("%s was not verified by %q", x.who, x.by)
		}
	}
	// Where both machines ended direct, packets must flow both ways.
	if c.wantA == modeDirect && c.wantB == modeDirect {
		if d, ok := waitFlow(a, b, 2*time.Second); !ok {
			t.Errorf("a to b does not carry packets after the trial (%v)", d)
		}
		if d, ok := waitFlow(b, a, 2*time.Second); !ok {
			t.Errorf("b to a does not carry packets after the trial (%v)", d)
		}
	}
	time.Sleep(80 * time.Millisecond) // the follower's timer, before the devices close
}

func TestTrialVerification_Scenarios(t *testing.T) {
	cases := []oneWayCase{
		{name: "healthy pair, a starts the handshake", leader: "a", aAnswers: true, bAnswers: true, wantA: modeDirect, wantB: modeDirect},
		{name: "healthy pair, b starts the handshake", leader: "b", aAnswers: true, bAnswers: true, wantA: modeDirect, wantB: modeDirect},
		// b's system answers nothing (stealth mode): a's probes are never answered, and both still verify.
		{name: "b is in stealth mode, a starts", leader: "a", aAnswers: true, bAnswers: false, wantA: modeDirect, wantB: modeDirect, byA: proofInbound},
		{name: "b is in stealth mode, b starts", leader: "b", aAnswers: true, bAnswers: false, wantA: modeDirect, wantB: modeDirect, byA: proofInbound},
		{name: "both in stealth mode", leader: "a", aAnswers: false, bAnswers: false, wantA: modeDirect, wantB: modeDirect, byA: proofInbound, byB: proofInbound},
		// The victim's inbound is dead, healed by adding the peer again. The healthy machine verifies
		// on the victim's packets and does nothing more; the victim adds the peer again alone, which
		// gives both a clean session, and then verifies.
		{name: "inbound of b is dead, healed by the retry; b started the handshake", leader: "b", aAnswers: true, bAnswers: true, victim: "b", heal: true,
			wantA: modeDirect, wantB: modeDirect, rekeyB: true, byA: proofInbound},
		{name: "inbound of b is dead, healed by the retry; a started the handshake", leader: "a", aAnswers: true, bAnswers: true, victim: "b", heal: true,
			wantA: modeDirect, wantB: modeDirect, rekeyB: true, byA: proofInbound},
		{name: "inbound of a is dead, healed by the retry", leader: "b", aAnswers: true, bAnswers: true, victim: "a", heal: true,
			wantA: modeDirect, wantB: modeDirect, rekeyA: true, byB: proofInbound},
		// A follower whose inbound is dead from the first packet never completes the handshake as the
		// responder (the leader's first keepalive is dropped), so at its hold it has no fresh handshake and
		// adds the peer again by itself: no retry of the verification is needed.
		{name: "inbound of the follower is dead from the first packet: its own hold repairs it", leader: "a", aAnswers: true, bAnswers: true, victim: "b", heal: true, faultFromStart: true,
			wantA: modeDirect, wantB: modeDirect},
		// The fault stays: the victim ends with NO DATA PATH and goes back to the relay; the other
		// machine verified on the victim's packets and stays labelled direct.
		{name: "inbound of b is dead and stays dead", leader: "a", aAnswers: true, bAnswers: true, victim: "b",
			wantA: modeDirect, wantB: modeDERP, rekeyB: true, noPathB: true, byA: proofInbound},
		// A pair that is one-way and in stealth mode at once: after the heal the victim has no evidence,
		// because the healthy machine verified earlier and no longer probes and its system answers
		// nothing. The victim goes back to the relay, which is the safe outcome; the healthy machine's
		// packets follow it there.
		{name: "inbound of b is dead, healed, and a is in stealth mode", leader: "a", aAnswers: false, bAnswers: true, victim: "b", heal: true,
			wantA: modeDirect, wantB: modeDERP, rekeyB: true, noPathB: true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { runOneWayCase(t, c) })
	}
}
