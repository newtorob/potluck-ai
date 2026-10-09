package main

import (
	"net/netip"
	"sync"
	"testing"
	"time"
)

// fakeWatch is an inboundWatcher whose counts the test moves.
type fakeWatch struct {
	mu      sync.Mutex
	counts  map[netip.Addr]uint64
	watched map[netip.Addr]bool
	watches int
	unwatch int
}

func newFakeWatch() *fakeWatch {
	return &fakeWatch{counts: map[netip.Addr]uint64{}, watched: map[netip.Addr]bool{}}
}

func (w *fakeWatch) WatchInbound(a netip.Addr) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.watches++
	w.watched[a] = true
}
func (w *fakeWatch) UnwatchInbound(a netip.Addr) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.unwatch++
	delete(w.watched, a)
	delete(w.counts, a)
}
func (w *fakeWatch) InboundCount(a netip.Addr) uint64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	if !w.watched[a] {
		return 0
	}
	return w.counts[a]
}

// packet is a packet from a that comes out of the tunnel; unwatched addresses are not counted, like the real filter.
func (w *fakeWatch) packet(a string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	ad := netip.MustParseAddr(a)
	if w.watched[ad] {
		w.counts[ad]++
	}
}

func (w *fakeWatch) isWatched(a string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.watched[netip.MustParseAddr(a)]
}

const peerIP = "100.64.0.2"

func newWatchedVerifier(answers []bool) (*trialVerifier, *fakeWatch, *fakeProbe, chan struct{}) {
	f := &fakeProbe{answers: answers}
	done := make(chan struct{}, 64)
	v := newTestVerifier(f, done)
	w := newFakeWatch()
	v.watch = w
	return v, w, f, done
}

func TestEvidence_InboundPacketVerifiesWithoutAnyProbeAnswer(t *testing.T) {
	v, w, _, done := newWatchedVerifier([]bool{false})
	v.begin("k", peerIP)
	if !w.isWatched(peerIP) {
		t.Fatal("begin must start watching the peer's address")
	}
	t0 := time.Now()
	if ok, _, _ := v.observe("k", peerIP, t0); ok {
		t.Fatal("verified with no evidence")
	}
	waitDone(t, done)
	if ok, _, _ := v.observe("k", peerIP, t0.Add(2*time.Second)); ok {
		t.Fatal("an unanswered probe verified")
	}
	waitDone(t, done)
	w.packet(peerIP) // the peer's SYN comes out of the tunnel; the peer's system answers nothing
	ok, _, _ := v.observe("k", peerIP, t0.Add(4*time.Second))
	if !ok {
		t.Fatal("an inbound packet from the peer did not verify")
	}
	if got := v.proof("k"); got != proofInbound {
		t.Errorf("proof = %q, want %q", got, proofInbound)
	}
}

func TestEvidence_ProbeAnswerVerifiesWithoutAnyInboundPacket(t *testing.T) {
	v, _, _, done := newWatchedVerifier([]bool{true})
	v.begin("k", peerIP)
	v.observe("k", peerIP, time.Now())
	waitDone(t, done)
	ok, _, _ := v.observe("k", peerIP, time.Now().Add(2*time.Second))
	if !ok || v.proof("k") != proofProbe {
		t.Fatalf("ok=%v proof=%q, want a probe answer to verify", ok, v.proof("k"))
	}
}

// Packets that came out of the tunnel before the peer was added (the trial's start or its retry)
// belong to a session that is gone; only later ones count.
func TestEvidence_OnlyPacketsAfterTheLastAddCount(t *testing.T) {
	v, w, _, done := newWatchedVerifier([]bool{false})
	w.watched[netip.MustParseAddr(peerIP)] = true
	w.counts[netip.MustParseAddr(peerIP)] = 5 // seen while the previous session carried traffic
	v.begin("k", peerIP)
	t0 := time.Now()
	if ok, _, _ := v.observe("k", peerIP, t0); ok {
		t.Fatal("packets counted before the trial began were taken as evidence")
	}
	waitDone(t, done)
	w.packet(peerIP)
	if ok, _, _ := v.observe("k", peerIP, t0.Add(2*time.Second)); !ok {
		t.Fatal("a packet after begin did not verify")
	}

	// The same at the retry: markRekeyed takes a new baseline, after the add.
	v2, w2, _, done2 := newWatchedVerifier([]bool{false})
	v2.begin("k", peerIP)
	w2.packet(peerIP)
	w2.packet(peerIP) // from the session that is about to be replaced
	v2.markRekeyed("k", t0)
	if ok, _, rk := v2.observe("k", peerIP, t0.Add(2*time.Second)); ok || !rk {
		t.Fatalf("evidence from before the retry counted: ok=%v rekeyed=%v", ok, rk)
	}
	waitDone(t, done2)
	w2.packet(peerIP)
	if ok, _, _ := v2.observe("k", peerIP, t0.Add(4*time.Second)); !ok {
		t.Fatal("a packet after the retry did not verify")
	}
}

// The first probe goes out even when inbound evidence is already there (the peer may be waiting for
// this machine's SYN), and no probe goes out after that once verified.
func TestEvidence_FirstProbeAlwaysGoesOut(t *testing.T) {
	v, w, f, done := newWatchedVerifier([]bool{false})
	v.begin("k", peerIP)
	w.packet(peerIP)
	t0 := time.Now()
	ok, _, _ := v.observe("k", peerIP, t0)
	if !ok {
		t.Fatal("not verified on the first sight although a packet had arrived")
	}
	waitDone(t, done)
	if n := f.calls; n != 1 {
		t.Errorf("%d probes at the first sight, want 1", n)
	}
	v.observe("k", peerIP, t0.Add(2*time.Second))
	v.observe("k", peerIP, t0.Add(4*time.Second))
	if n := f.calls; n != 1 {
		t.Errorf("%d probes after verification, want no more than the first", n)
	}
}

func TestEvidence_WatchFollowsTheTrial(t *testing.T) {
	v, w, _, done := newWatchedVerifier([]bool{false})
	v.begin("a", "100.64.0.2")
	v.begin("b", "100.64.0.3")
	if !w.isWatched("100.64.0.2") || !w.isWatched("100.64.0.3") {
		t.Fatal("both trial peers must be watched")
	}
	v.begin("a", "100.64.0.2") // a new trial for the same peer: still one watch on it
	v.finish("a")
	if w.isWatched("100.64.0.2") {
		t.Error("finish must stop watching")
	}
	if !w.isWatched("100.64.0.3") {
		t.Error("finishing one peer stopped the other's watch")
	}
	v.prune(map[string]bool{}) // b left the roster
	if w.isWatched("100.64.0.3") {
		t.Error("prune must stop watching peers that left")
	}
	v.finish("never-begun") // must not create state or panic
	if _, ok := v.peers["never-begun"]; ok {
		t.Error("finish created state for a peer that never began")
	}
	_ = done
}

func TestEvidence_NoWatcherMeansProbeOnly(t *testing.T) {
	f := &fakeProbe{answers: []bool{false}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done) // watch is nil
	v.begin("k", peerIP)
	v.observe("k", peerIP, time.Now())
	waitDone(t, done)
	if ok, _, _ := v.observe("k", peerIP, time.Now().Add(2*time.Second)); ok {
		t.Error("verified with no watcher and no probe answer")
	}
}

// ---------------------------------------------------------------- decision table

// scenario drives trialStepFor tick by tick (2 s apart) with the handshake fresh from the first tick on.
type scenario struct {
	name string
	// probeAnswered: the peer's system answers a SYN to a closed port.
	probeAnswered bool
	// inboundFrom: the verify tick from which packets from the peer arrive (0 is the first tick), or -1 for never.
	inboundFrom int
	// inboundAfterRekey: packets from the peer arrive only after this machine added the peer again.
	inboundAfterRekey bool
	want              trialStep // the last step before the trial ends
	wantBy            string
	wantRekeyed       bool
}

func runScenario(t *testing.T, sc scenario) (final trialStep, by string, rekeyed bool, steps []trialStep) {
	t.Helper()
	f := &fakeProbe{answers: []bool{sc.probeAnswered}}
	done := make(chan struct{}, 64)
	v := newTestVerifier(f, done)
	w := newFakeWatch()
	v.watch = w
	v.begin("k", peerIP)
	t0 := time.Now()
	deadline := t0.Add(punchTrialDuration)
	for tick := 0; tick < 7; tick++ {
		now := t0.Add(time.Duration(tick) * punchVerifyInterval)
		inbound := sc.inboundFrom >= 0 && tick >= sc.inboundFrom && (!sc.inboundAfterRekey || rekeyed)
		if inbound {
			w.packet(peerIP)
		}
		before := probesStarted(v, "k")
		step := trialStepFor(v, "k", peerIP, trialVerified, deadline, now)
		if probesStarted(v, "k") > before {
			waitDone(t, done)
		}
		steps = append(steps, step)
		switch step {
		case stepVerified:
			return step, v.proof("k"), rekeyed, steps
		case stepRekey:
			rekeyed = true
			v.markRekeyed("k", now)
		case stepNoPath:
			return step, v.proof("k"), rekeyed, steps
		}
	}
	return steps[len(steps)-1], v.proof("k"), rekeyed, steps
}

func probesStarted(v *trialVerifier, k string) int {
	v.mu.Lock()
	defer v.mu.Unlock()
	if st := v.peers[k]; st != nil {
		return st.probes
	}
	return 0
}

func TestEvidence_DecisionTable(t *testing.T) {
	cases := []scenario{
		// new sidecar on both machines
		{name: "new/new, the peer's system answers resets, packets flow", probeAnswered: true, inboundFrom: 0, want: stepVerified, wantBy: proofInbound},
		{name: "new/new, the peer's system answers resets, its SYNs are late", probeAnswered: true, inboundFrom: 5, want: stepVerified, wantBy: proofProbe},
		{name: "new/new, the peer is in stealth mode, its SYNs arrive", probeAnswered: false, inboundFrom: 0, want: stepVerified, wantBy: proofInbound},
		{name: "new/new, the peer is in stealth mode, its SYNs arrive a tick later", probeAnswered: false, inboundFrom: 1, want: stepVerified, wantBy: proofInbound},
		// a session that works one way only, seen from the machine whose inbound is dead
		{name: "one-way, this machine's inbound is dead, the peer's system would answer", probeAnswered: false, inboundFrom: -1, want: stepNoPath, wantRekeyed: true},
		{name: "one-way, this machine's inbound is dead, healed by the retry, the peer's system answers", probeAnswered: false, inboundFrom: 3, inboundAfterRekey: true, want: stepVerified, wantBy: proofInbound, wantRekeyed: true},
		// new sidecar with an older one: the older one sends no probes
		{name: "new/old, the old peer's system answers resets", probeAnswered: true, inboundFrom: -1, want: stepVerified, wantBy: proofProbe},
		{name: "new/old, the old peer is in stealth mode and nothing arrives", probeAnswered: false, inboundFrom: -1, want: stepNoPath, wantRekeyed: true},
		{name: "new/old, the old peer is in stealth mode and real traffic arrives", probeAnswered: false, inboundFrom: 2, want: stepVerified, wantBy: proofInbound},
	}
	for _, sc := range cases {
		t.Run(sc.name, func(t *testing.T) {
			final, by, rekeyed, steps := runScenario(t, sc)
			if final != sc.want {
				t.Errorf("ended with %v, want %v (steps %v)", final, sc.want, steps)
			}
			if sc.want == stepVerified && by != sc.wantBy {
				t.Errorf("verified by %q, want %q", by, sc.wantBy)
			}
			if rekeyed != sc.wantRekeyed {
				t.Errorf("peer added again = %v, want %v (steps %v)", rekeyed, sc.wantRekeyed, steps)
			}
			// The end of the trial has to come inside the trial window.
			if len(steps) > int(punchTrialDuration/punchVerifyInterval) {
				t.Errorf("took %d ticks", len(steps))
			}
		})
	}
}

// A handshake that is not fresh is never verified, whatever else has been seen.
func TestEvidence_NoFreshHandshakeNeverVerifies(t *testing.T) {
	v, w, _, done := newWatchedVerifier([]bool{true})
	v.begin("k", peerIP)
	w.packet(peerIP)
	now := time.Now()
	for _, hs := range []lanTrialOutcome{trialPending, trialTimedOut} {
		if got := trialStepFor(v, "k", peerIP, hs, now.Add(time.Hour), now); got == stepVerified {
			t.Errorf("handshake outcome %v verified", hs)
		}
	}
	_ = done
}
