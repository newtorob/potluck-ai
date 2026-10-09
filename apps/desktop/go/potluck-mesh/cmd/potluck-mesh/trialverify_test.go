package main

import (
	"crypto/rand"
	"encoding/base64"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
)

func b64key(b byte) string {
	k := [32]byte{}
	k[0] = b
	return base64.StdEncoding.EncodeToString(k[:])
}

// ---------------------------------------------------------------- trialLeader

func TestTrialLeader_ExactlyOneOfAPairStartsTheHandshake(t *testing.T) {
	for i := 0; i < 200; i++ {
		var a, b [32]byte
		_, _ = rand.Read(a[:])
		_, _ = rand.Read(b[:])
		ka, kb := base64.StdEncoding.EncodeToString(a[:]), base64.StdEncoding.EncodeToString(b[:])
		if ka == kb {
			continue
		}
		if trialLeader(ka, kb) == trialLeader(kb, ka) {
			t.Fatalf("both or neither of %q and %q start the handshake", ka, kb)
		}
	}
}

func TestTrialLeader_LowerKeyStarts(t *testing.T) {
	if !trialLeader(b64key(1), b64key(2)) {
		t.Error("the lower key must start the handshake")
	}
	if trialLeader(b64key(2), b64key(1)) {
		t.Error("the higher key must wait")
	}
	// Compared as bytes, not as base64 text: 0xfb... is "+..." in base64 and sorts
	// below "A..." as text, but above 0x01 as bytes.
	hi := [32]byte{0xfb}
	lo := [32]byte{0x01}
	khi, klo := base64.StdEncoding.EncodeToString(hi[:]), base64.StdEncoding.EncodeToString(lo[:])
	if trialLeader(khi, klo) || !trialLeader(klo, khi) {
		t.Error("keys must be compared as bytes")
	}
}

func TestTrialLeader_UnreadableKeysMeanStartAsBefore(t *testing.T) {
	for _, c := range [][2]string{{"", b64key(1)}, {b64key(1), ""}, {"not base64!", b64key(1)}, {b64key(1), "AAAA"}, {"", ""}} {
		if !trialLeader(c[0], c[1]) {
			t.Errorf("trialLeader(%q, %q) = false, want true (the behaviour before the change)", c[0], c[1])
		}
	}
}

// ---------------------------------------------------------------- stepTrial

func TestStepTrial(t *testing.T) {
	now := time.Now()
	deadline := now.Add(punchTrialDuration)
	past := now.Add(-time.Second)
	fresh := func(age time.Duration) time.Time { return now.Add(-age) }

	cases := []struct {
		name       string
		handshake  lanTrialOutcome
		probeOK    bool
		firstFresh time.Time
		rekeyed    bool
		now        time.Time
		deadline   time.Time
		want       trialStep
	}{
		{"no handshake yet", trialPending, false, time.Time{}, false, now, deadline, stepPending},
		{"no handshake, deadline passed", trialTimedOut, false, time.Time{}, false, now, past, stepTimedOut},
		{"handshake only, first sight: wait for the probe", trialVerified, false, now, false, now, deadline, stepPending},
		{"handshake and the probe was answered", trialVerified, true, now, false, now, deadline, stepVerified},
		{"probe answered at the first sight", trialVerified, true, time.Time{}, false, now, deadline, stepVerified},
		{"handshake only, one tick later: still waiting", trialVerified, false, fresh(2 * time.Second), false, now, deadline, stepPending},
		{"handshake only, long enough: add the peer again", trialVerified, false, fresh(trialRekeyAfter), false, now, deadline, stepRekey},
		{"handshake only, after the one retry, one tick: wait", trialVerified, false, fresh(punchVerifyInterval), true, now, deadline, stepPending},
		{"handshake only, after the retry, long enough: give up", trialVerified, false, fresh(trialNoPathAfter), true, now, deadline, stepNoPath},
		{"handshake only, never added again, very long: give up", trialVerified, false, fresh(trialNoPathAfter + time.Second), true, now, deadline, stepNoPath},
		{"handshake only at the deadline: give up, do not verify", trialVerified, false, fresh(time.Second), false, now, past, stepNoPath},
		{"probe answered at the deadline still verifies", trialVerified, true, fresh(time.Second), false, now, past, stepVerified},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := stepTrial(c.handshake, c.probeOK, c.firstFresh, c.rekeyed, c.deadline, c.now); got != c.want {
				t.Errorf("got %v, want %v", got, c.want)
			}
		})
	}
}

// The central property: with no answer to the probe, no input makes a trial
// verified, whatever the handshake and the clock say.
func TestStepTrial_NeverVerifiesWithoutTheProbe(t *testing.T) {
	now := time.Now()
	for _, hs := range []lanTrialOutcome{trialPending, trialVerified, trialTimedOut} {
		for _, rekeyed := range []bool{false, true} {
			for age := time.Duration(0); age < 40*time.Second; age += 500 * time.Millisecond {
				for _, dl := range []time.Time{now.Add(time.Hour), now, now.Add(-time.Hour)} {
					if got := stepTrial(hs, false, now.Add(-age), rekeyed, dl, now); got == stepVerified {
						t.Fatalf("verified without the probe: handshake=%v rekeyed=%v age=%v deadline=%v", hs, rekeyed, age, dl.Sub(now))
					}
				}
			}
		}
	}
}

// The give-up point has to fit in the trial window together with one retry, or the
// retry would never get a chance. A fresh handshake is first seen at most one verify
// tick after the trial starts; each wait below ends on the third tick after it began.
func TestTrialTimingFitsInTheWindow(t *testing.T) {
	// Between one and two ticks: the check runs on every tick, so the wait ends on the third
	// tick after it began (two probes made and failed). Exactly two ticks would end on the
	// third or the fourth depending on the timer's jitter.
	for name, d := range map[string]time.Duration{"trialRekeyAfter": trialRekeyAfter, "trialNoPathAfter": trialNoPathAfter} {
		if d <= punchVerifyInterval || d >= 2*punchVerifyInterval {
			t.Errorf("%s (%v) must be between one and two verify ticks (%v and %v)", name, d, punchVerifyInterval, 2*punchVerifyInterval)
		}
	}
	worst := punchVerifyInterval + 2*punchVerifyInterval + 2*punchVerifyInterval
	if worst > punchTrialDuration {
		t.Errorf("a dead path ends the trial after %v at worst, longer than the trial window %v", worst, punchTrialDuration)
	}
	if punchFollowerDelay >= punchTrialDuration/4 {
		t.Errorf("punchFollowerDelay %v is too long for a trial window of %v", punchFollowerDelay, punchTrialDuration)
	}
}

// ---------------------------------------------------------------- probeTunnelPath

func TestProbeTunnelPath_ListenerCountsAsAnswer(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Skipf("no loopback: %v", err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			_ = c.Close()
		}
	}()
	port := ln.Addr().(*net.TCPAddr).Port
	if !probePort("127.0.0.1", "127.0.0.1", port, time.Second) {
		t.Error("an accepted connect must count as an answer")
	}
}

func TestProbeTunnelPath_RefusalCountsAsAnswer(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Skipf("no loopback: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close() // nothing listens on port any more: the stack answers with a reset
	if !probePort("127.0.0.1", "127.0.0.1", port, time.Second) {
		t.Error("a refused connect must count as an answer (the peer's stack replied)")
	}
}

func TestProbeTunnelPath_NoAnswerIsFalse(t *testing.T) {
	// 192.0.2.0/24 is TEST-NET-1: nothing answers. Depending on the machine the
	// connect times out or fails at once with no route; neither is an answer.
	start := time.Now()
	if probeTunnelPath("127.0.0.1", "192.0.2.1", 300*time.Millisecond) {
		t.Error("a connect that gets no answer must be false")
	}
	if time.Since(start) > 3*time.Second {
		t.Errorf("the probe ignored its timeout: %v", time.Since(start))
	}
	for _, c := range [][2]string{{"", "100.64.0.2"}, {"100.64.0.1", ""}, {"junk", "100.64.0.2"}, {"100.64.0.1", "junk"}} {
		if probeTunnelPath(c[0], c[1], 100*time.Millisecond) {
			t.Errorf("probeTunnelPath(%q, %q) = true for an unusable address", c[0], c[1])
		}
	}
}

// ---------------------------------------------------------------- trialVerifier

// fakeProbe answers probes from a script and lets the test wait for each one.
type fakeProbe struct {
	mu      sync.Mutex
	answers []bool // consumed in order; the last repeats
	calls   int32
	gate    chan struct{} // when non-nil, each probe waits for a value here
}

func (f *fakeProbe) fn(_, _ string, _ time.Duration) bool {
	atomic.AddInt32(&f.calls, 1)
	if f.gate != nil {
		<-f.gate
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.answers) == 0 {
		return false
	}
	a := f.answers[0]
	if len(f.answers) > 1 {
		f.answers = f.answers[1:]
	}
	return a
}

func newTestVerifier(f *fakeProbe, done chan struct{}) *trialVerifier {
	v := newTrialVerifier(func() string { return "100.64.0.1" }, nil)
	v.probe = f.fn
	if done != nil {
		v.done = func() { done <- struct{}{} }
	}
	return v
}

func waitDone(t *testing.T, done chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("the probe did not finish")
	}
}

func TestTrialVerifier_FirstSightStartsOneProbeAndKeepsTheTime(t *testing.T) {
	f := &fakeProbe{answers: []bool{false}, gate: make(chan struct{})}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	t0 := time.Now()
	ok, first, rekeyed := v.observe("k", "100.64.0.2", t0)
	if ok || rekeyed || !first.Equal(t0) {
		t.Fatalf("first sight: ok=%v first=%v rekeyed=%v", ok, first, rekeyed)
	}
	// While the probe runs, further ticks must not start another and must keep the time.
	_, first2, _ := v.observe("k", "100.64.0.2", t0.Add(3*time.Second))
	if !first2.Equal(t0) {
		t.Errorf("the first-sight time moved: %v", first2.Sub(t0))
	}
	f.gate <- struct{}{}
	waitDone(t, done)
	if n := atomic.LoadInt32(&f.calls); n != 1 {
		t.Errorf("%d probes ran, want 1", n)
	}
}

func TestTrialVerifier_ProbeIntervalAndStickyAnswer(t *testing.T) {
	f := &fakeProbe{answers: []bool{false, true}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	t0 := time.Now()
	v.observe("k", "100.64.0.2", t0)
	waitDone(t, done)
	// Too soon after the last probe: no new one.
	v.observe("k", "100.64.0.2", t0.Add(trialProbeInterval/2))
	if n := atomic.LoadInt32(&f.calls); n != 1 {
		t.Fatalf("a probe started %v after the last one (calls=%d)", trialProbeInterval/2, n)
	}
	// Old enough: the second probe runs and succeeds.
	v.observe("k", "100.64.0.2", t0.Add(2*time.Second))
	waitDone(t, done)
	ok, _, _ := v.observe("k", "100.64.0.2", t0.Add(4*time.Second))
	if !ok {
		t.Fatal("the answered probe was not recorded")
	}
	// Once answered, no more probes.
	v.observe("k", "100.64.0.2", t0.Add(6*time.Second))
	if n := atomic.LoadInt32(&f.calls); n != 2 {
		t.Errorf("probes kept running after an answer (calls=%d)", n)
	}
}

func TestTrialVerifier_BeginDropsAnAnswerThatArrivesLate(t *testing.T) {
	f := &fakeProbe{answers: []bool{true}, gate: make(chan struct{})}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	v.observe("k", "100.64.0.2", time.Now())
	v.begin("k", "100.64.0.2") // a new trial starts for the same peer while the old probe is in flight
	f.gate <- struct{}{}
	waitDone(t, done)
	ok, first, rekeyed := v.observe("k", "100.64.0.2", time.Now())
	if ok {
		t.Error("an answer from before the new trial began was counted for it")
	}
	if rekeyed || first.IsZero() {
		t.Errorf("state after begin: rekeyed=%v first=%v", rekeyed, first)
	}
}

func TestTrialVerifier_MarkRekeyedStartsTheClockAtTheRetry(t *testing.T) {
	f := &fakeProbe{answers: []bool{false}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	t0 := time.Now()
	v.observe("k", "100.64.0.2", t0)
	waitDone(t, done)
	t1 := t0.Add(5 * time.Second)
	v.markRekeyed("k", t1)
	_, first, rekeyed := v.observe("k", "100.64.0.2", t1.Add(2*time.Second))
	if !rekeyed {
		t.Error("rekeyed flag lost")
	}
	if !first.Equal(t1) {
		t.Errorf("the clock must run from the retry itself, not from the next sight of a handshake: first = retry + %v", first.Sub(t1))
	}
}

func TestTrialVerifier_PeersAreIndependent(t *testing.T) {
	f := &fakeProbe{answers: []bool{true}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	now := time.Now()
	v.observe("a", "100.64.0.2", now)
	waitDone(t, done)
	if ok, _, _ := v.observe("a", "100.64.0.2", now); !ok {
		t.Fatal("a not answered")
	}
	if ok, _, _ := v.observe("b", "100.64.0.3", now); ok {
		t.Error("b took a's answer")
	}
	waitDone(t, done)
}

func TestTrialStepFor_NoVerifierOrNothingToProbeUsesTheHandshakeAlone(t *testing.T) {
	now := time.Now()
	dl := now.Add(time.Minute)
	var nilV *trialVerifier
	if got := trialStepFor(nilV, "k", "100.64.0.2", trialVerified, dl, now); got != stepVerified {
		t.Errorf("nil verifier: %v, want verified", got)
	}
	noIP := newTrialVerifier(func() string { return "" }, nil)
	if got := trialStepFor(noIP, "k", "100.64.0.2", trialVerified, dl, now); got != stepVerified {
		t.Errorf("own mesh address unknown: %v, want verified", got)
	}
	v := newTrialVerifier(func() string { return "100.64.0.1" }, nil)
	if got := trialStepFor(v, "k", "", trialVerified, dl, now); got != stepVerified {
		t.Errorf("peer mesh address unknown: %v, want verified", got)
	}
	if got := trialStepFor(nilV, "k", "100.64.0.2", trialPending, dl, now); got != stepPending {
		t.Errorf("pending: %v", got)
	}
	if got := trialStepFor(nilV, "k", "100.64.0.2", trialTimedOut, dl, now); got != stepTimedOut {
		t.Errorf("timed out: %v", got)
	}
}

func TestTrialStepFor_HandshakeAloneDoesNotVerifyWithAVerifier(t *testing.T) {
	f := &fakeProbe{answers: []bool{false}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	now := time.Now()
	dl := now.Add(punchTrialDuration)
	if got := trialStepFor(v, "k", "100.64.0.2", trialVerified, dl, now); got != stepPending {
		t.Fatalf("first sight with a silent peer: %v, want pending", got)
	}
	waitDone(t, done)
	if got := trialStepFor(v, "k", "100.64.0.2", trialVerified, dl, now.Add(trialRekeyAfter)); got != stepRekey {
		t.Errorf("after trialRekeyAfter: %v, want rekey", got)
	}
	t1 := now.Add(trialRekeyAfter + time.Second)
	v.markRekeyed("k", t1)
	if got := trialStepFor(v, "k", "100.64.0.2", trialVerified, dl, t1); got != stepPending {
		t.Fatalf("first sight after the retry: %v, want pending", got)
	}
	waitDone(t, done)
	if got := trialStepFor(v, "k", "100.64.0.2", trialVerified, dl, t1.Add(trialNoPathAfter)); got != stepNoPath {
		t.Errorf("after trialNoPathAfter: %v, want no-path", got)
	}
}

// ---------------------------------------------------------------- OnPunchCoord

func punchFixtureWithKeys(t *testing.T, self byte, followerDelay time.Duration) (*rosterHandler, *recoveryTunnel, msignal.RosterPeer) {
	t.Helper()
	h, tun, _, p, _ := recoveryFixture(t)
	// recoveryFixture's peer key is [32]byte{1}; self is chosen on either side of it.
	h.selfKeyFn = func() string { return b64key(self) }
	h.followerDelay = followerDelay
	return h, tun, p
}

func punchCoordFor(p msignal.RosterPeer) msignal.PunchCoord {
	return msignal.PunchCoord{PeerNodeID: p.NodeID, PeerWGPublicKey: p.WGPublicKey, PeerEndpoint: p.LastEndpoint, Role: "initiator"}
}

// recoveryTunnel records AddPeer calls under no lock; the follower's delayed call comes from another
// goroutine, so these tests read it after waiting for it.
func waitAdded(t *testing.T, tun *recoveryTunnel, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		tun.mu.Lock()
		got := len(tun.added)
		tun.mu.Unlock()
		if got >= n {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d AddPeer calls, want %d", got, n)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestOnPunchCoord_LeaderStartsTheHandshakeAtOnce(t *testing.T) {
	h, tun, p := punchFixtureWithKeys(t, 0, time.Hour) // key 0 < key 1: this machine starts it
	h.OnPunchCoord(punchCoordFor(p))
	tun.mu.Lock()
	defer tun.mu.Unlock()
	if len(tun.added) != 1 {
		t.Fatalf("%d AddPeer calls, want 1", len(tun.added))
	}
	if got := tun.added[0].PersistentKeepalive; got != punchTrialKeepalive {
		t.Errorf("leader keepalive %d, want %d (the keepalive is what makes wireguard-go send the initiation)", got, punchTrialKeepalive)
	}
	if tun.added[0].Endpoint != "192.168.1.20:51820" {
		t.Errorf("endpoint %q", tun.added[0].Endpoint)
	}
}

func TestOnPunchCoord_FollowerCannotSendAtFirst(t *testing.T) {
	h, tun, p := punchFixtureWithKeys(t, 2, time.Hour) // key 2 > key 1: waits
	h.OnPunchCoord(punchCoordFor(p))
	tun.mu.Lock()
	defer tun.mu.Unlock()
	if len(tun.added) != 1 {
		t.Fatalf("%d AddPeer calls straight after the punch, want 1", len(tun.added))
	}
	first := tun.added[0]
	if first.PersistentKeepalive != 0 {
		t.Errorf("follower keepalive %d at the start, want 0 (any keepalive makes wireguard-go send an initiation)", first.PersistentKeepalive)
	}
	if first.Endpoint != "" {
		t.Errorf("follower endpoint %q at the start, want none (with an endpoint, a packet the machine sends to the peer starts a handshake that crosses the first machine's)", first.Endpoint)
	}
	if len(first.AllowedIPs) != 1 || first.AllowedIPs[0] != p.MeshIP+"/32" {
		t.Errorf("follower peer lost its allowed-ips: %+v", first)
	}
	if h.modes.get(p.WGPublicKey) != modePunchTrial {
		t.Error("follower must be in the trial")
	}
	st, ok := h.punchTrials.get(p.WGPublicKey)
	if !ok || st.endpoint != "192.168.1.20:51820" {
		t.Errorf("the trial must remember the endpoint for the later add: %+v", st)
	}
}

// The other machine's handshake has not got through when the delay ends: add the peer again,
// pinned, with the keepalive, so this machine's own initiation goes out.
func TestOnPunchCoord_FollowerStartsItsOwnHandshakeWhenNoneArrived(t *testing.T) {
	h, tun, p := punchFixtureWithKeys(t, 2, 40*time.Millisecond)
	h.OnPunchCoord(punchCoordFor(p)) // the fake reports the pre-trial handshake time and no newer one
	waitAdded(t, tun, 2)
	tun.mu.Lock()
	defer tun.mu.Unlock()
	second := tun.added[1]
	if second.PersistentKeepalive != punchTrialKeepalive || second.Endpoint != "192.168.1.20:51820" {
		t.Errorf("second add %+v, want the trial keepalive pinned to the endpoint", second)
	}
	if tun.removed != 2 {
		t.Errorf("%d RemovePeer calls, want 2 (the first at the start, the second so the new peer sends its initiation at once)", tun.removed)
	}
}

// The other machine's handshake got through: only set the keepalive, leave the session and the
// endpoint it taught this machine alone.
func TestOnPunchCoord_FollowerKeepsTheSessionThatCameUp(t *testing.T) {
	h, tun, p := punchFixtureWithKeys(t, 2, 40*time.Millisecond)
	h.OnPunchCoord(punchCoordFor(p))
	tun.mu.Lock()
	tun.handshake = time.Now().Add(time.Second) // newer than the pre-trial one, and fresh
	tun.mu.Unlock()
	waitAdded(t, tun, 2)
	tun.mu.Lock()
	defer tun.mu.Unlock()
	second := tun.added[1]
	if second.PersistentKeepalive != punchTrialKeepalive {
		t.Errorf("keepalive %d, want %d", second.PersistentKeepalive, punchTrialKeepalive)
	}
	if second.Endpoint != "" {
		t.Errorf("endpoint %q: must be left as the first machine's packet set it", second.Endpoint)
	}
	if tun.removed != 1 {
		t.Errorf("%d RemovePeer calls, want only the first: the session that came up must not be cleared", tun.removed)
	}
}

func TestOnPunchCoord_FollowerDoesNothingWhenTheTrialIsOver(t *testing.T) {
	for _, name := range []string{"mode reset by a rejoin", "peer gone", "a newer trial"} {
		t.Run(name, func(t *testing.T) {
			h, tun, p := punchFixtureWithKeys(t, 2, 60*time.Millisecond)
			h.OnPunchCoord(punchCoordFor(p))
			switch name {
			case "mode reset by a rejoin":
				h.modes.set(p.WGPublicKey, modeDERP)
			case "peer gone":
				h.punchTrials.remove(p.WGPublicKey)
			case "a newer trial":
				h.punchTrials.startTrial(p.WGPublicKey, "192.168.1.20:51820", time.Time{}, time.Now().Add(time.Hour))
			}
			time.Sleep(200 * time.Millisecond)
			tun.mu.Lock()
			defer tun.mu.Unlock()
			if len(tun.added) != 1 {
				t.Errorf("%d AddPeer calls, want only the first", len(tun.added))
			}
		})
	}
}

func TestOnPunchCoord_NoKeyKnownStartsAsBefore(t *testing.T) {
	h, tun, _, p, _ := recoveryFixture(t) // ipcState nil, selfKeyFn nil
	h.OnPunchCoord(punchCoordFor(p))
	tun.mu.Lock()
	defer tun.mu.Unlock()
	if len(tun.added) != 1 || tun.added[0].PersistentKeepalive != punchTrialKeepalive {
		t.Fatalf("with no key known the trial must start as it did before: %+v", tun.added)
	}
}

func TestOnPunchCoord_StartResetsTheProbeState(t *testing.T) {
	h, _, p := punchFixtureWithKeys(t, 0, time.Hour)
	f := &fakeProbe{answers: []bool{true}}
	done := make(chan struct{}, 16)
	h.verifier = newTestVerifier(f, done)
	h.verifier.observe(p.WGPublicKey, p.MeshIP, time.Now())
	waitDone(t, done)
	if ok, _, _ := h.verifier.observe(p.WGPublicKey, p.MeshIP, time.Now()); !ok {
		t.Fatal("setup: no answer recorded")
	}
	h.OnPunchCoord(punchCoordFor(p))
	if ok, _, _ := h.verifier.observe(p.WGPublicKey, p.MeshIP, time.Now()); ok {
		t.Error("an answer from before the trial counted for the new one")
	}
}

// No relay configured: a punch_coord only updates the endpoint, as before. No trial, no peer
// removed or added, no timer.
func TestOnPunchCoord_NoRelayIsUnchanged(t *testing.T) {
	h, tun, p := punchFixtureWithKeys(t, 2, 20*time.Millisecond) // would be a follower with a relay
	h.dBind = nil
	h.OnPunchCoord(punchCoordFor(p))
	time.Sleep(100 * time.Millisecond)
	tun.mu.Lock()
	defer tun.mu.Unlock()
	if len(tun.added) != 0 || tun.removed != 0 {
		t.Errorf("no relay: %d AddPeer, %d RemovePeer calls, want none", len(tun.added), tun.removed)
	}
	if h.modes.get(p.WGPublicKey) != modeDERP {
		t.Errorf("no relay: mode %v, want derp (unmanaged)", h.modes.get(p.WGPublicKey))
	}
}

func TestTrialVerifier_PruneForgetsPeersThatLeft(t *testing.T) {
	f := &fakeProbe{answers: []bool{true}}
	done := make(chan struct{}, 16)
	v := newTestVerifier(f, done)
	now := time.Now()
	v.observe("stay", "100.64.0.2", now)
	waitDone(t, done)
	v.observe("go", "100.64.0.3", now)
	waitDone(t, done)
	v.prune(map[string]bool{"stay": true})
	if ok, _, _ := v.observe("stay", "100.64.0.2", now); !ok {
		t.Error("a peer still in the roster lost its state")
	}
	if ok, first, _ := v.observe("go", "100.64.0.3", now.Add(time.Minute)); ok || !first.Equal(now.Add(time.Minute)) {
		t.Errorf("a peer that left kept its state: ok=%v first=%v", ok, first)
	}
	waitDone(t, done)
	var nilV *trialVerifier
	nilV.prune(nil) // must not panic
}
