package main

import (
	"net"
	"testing"
	"time"
)

// mustIPNet builds a net.IPNet from a CIDR for test fixtures, using the IP +
// the CIDR mask (the form LocalIPNets produces: IP is the interface address,
// Mask is the interface mask).
func mustIPNet(t *testing.T, cidr string) net.IPNet {
	t.Helper()
	ip, ipnet, err := net.ParseCIDR(cidr)
	if err != nil {
		t.Fatalf("bad CIDR %q: %v", cidr, err)
	}
	return net.IPNet{IP: ip.To4(), Mask: ipnet.Mask}
}

// ---------------------------------------------------------------------------
// sameLANEndpoint
// ---------------------------------------------------------------------------

func TestSameLANEndpoint_CoSubnetMatch(t *testing.T) {
	// My interface: 192.168.1.10/24. Peer advertises 192.168.1.20:51820 — same
	// /24, so it must match.
	myNets := []net.IPNet{mustIPNet(t, "192.168.1.10/24")}
	peerEps := []string{"192.168.1.20:51820"}

	got, ok := sameLANEndpoint(myNets, peerEps)
	if !ok {
		t.Fatalf("expected co-subnet match, got none")
	}
	if got != "192.168.1.20:51820" {
		t.Errorf("got %q, want 192.168.1.20:51820", got)
	}
}

func TestSameLANEndpoint_DifferentSubnetReject(t *testing.T) {
	// My interface: 192.168.1.10/24. Peer is on 192.168.2.20 — shares the /16
	// prefix but is NOT co-subnet under the /24 mask. Must reject.
	myNets := []net.IPNet{mustIPNet(t, "192.168.1.10/24")}
	peerEps := []string{"192.168.2.20:51820"}

	if got, ok := sameLANEndpoint(myNets, peerEps); ok {
		t.Errorf("expected reject (different /24), got match %q", got)
	}
}

func TestSameLANEndpoint_HairpinSameAddressReject(t *testing.T) {
	// Hairpin / same-public-different-subnet: the peer advertises OUR OWN
	// address. Routing at our own IP is never a usable direct path. Reject even
	// though it trivially "co-subnets" with itself.
	myNets := []net.IPNet{mustIPNet(t, "192.168.1.10/24")}
	peerEps := []string{"192.168.1.10:51820"}

	if got, ok := sameLANEndpoint(myNets, peerEps); ok {
		t.Errorf("expected reject (hairpin/self address), got match %q", got)
	}
}

func TestSameLANEndpoint_DifferentPrivateRangeReject(t *testing.T) {
	// Two hosts on entirely different RFC1918 ranges behind separate NATs that
	// happen to both be "private" — e.g. one on 10.x, the peer on 192.168.x.
	// Not co-subnet; reject.
	myNets := []net.IPNet{mustIPNet(t, "10.0.0.5/8")}
	peerEps := []string{"192.168.1.20:51820"}

	if got, ok := sameLANEndpoint(myNets, peerEps); ok {
		t.Errorf("expected reject (different private range), got match %q", got)
	}
}

func TestSameLANEndpoint_FirstCoSubnetWinsAmongMany(t *testing.T) {
	// Multiple candidates; the first one that co-subnets with any of my nets is
	// returned. Here the first candidate is off-subnet, the second matches.
	myNets := []net.IPNet{mustIPNet(t, "192.168.1.10/24")}
	peerEps := []string{"10.5.5.5:51820", "192.168.1.77:51820"}

	got, ok := sameLANEndpoint(myNets, peerEps)
	if !ok {
		t.Fatalf("expected a match among candidates")
	}
	if got != "192.168.1.77:51820" {
		t.Errorf("got %q, want 192.168.1.77:51820", got)
	}
}

func TestSameLANEndpoint_MultiHomedMatchesSecondInterface(t *testing.T) {
	// Multi-homed host: wired 10.0.0.0/24 + WiFi 192.168.1.0/24. Peer is on the
	// WiFi subnet only — must match against the second interface.
	myNets := []net.IPNet{
		mustIPNet(t, "10.0.0.2/24"),
		mustIPNet(t, "192.168.1.2/24"),
	}
	peerEps := []string{"192.168.1.50:51820"}

	got, ok := sameLANEndpoint(myNets, peerEps)
	if !ok || got != "192.168.1.50:51820" {
		t.Fatalf("expected match on second interface, got (%q, %v)", got, ok)
	}
}

func TestSameLANEndpoint_EmptyInputs(t *testing.T) {
	if _, ok := sameLANEndpoint(nil, []string{"192.168.1.20:51820"}); ok {
		t.Errorf("expected no match with no local nets")
	}
	if _, ok := sameLANEndpoint([]net.IPNet{mustIPNet(t, "192.168.1.10/24")}, nil); ok {
		t.Errorf("expected no match with no peer endpoints")
	}
}

func TestSameLANEndpoint_MalformedCandidatesSkipped(t *testing.T) {
	// Garbage candidates must be skipped, not crash; a valid one after them
	// still matches.
	myNets := []net.IPNet{mustIPNet(t, "192.168.1.10/24")}
	peerEps := []string{"not-an-endpoint", "192.168.1.99", "192.168.1.40:51820"}

	got, ok := sameLANEndpoint(myNets, peerEps)
	if !ok || got != "192.168.1.40:51820" {
		t.Fatalf("expected to skip malformed and match the valid one, got (%q, %v)", got, ok)
	}
}

// ---------------------------------------------------------------------------
// shouldStartLANTrial — derp→trial gate
// ---------------------------------------------------------------------------

func TestShouldStartLANTrial(t *testing.T) {
	now := time.Now()
	past := now.Add(-time.Minute)
	future := now.Add(time.Minute)

	cases := []struct {
		name         string
		enabled      bool
		mode         routingMode
		hasSameLANEp bool
		backoffUntil time.Time
		want         bool
	}{
		{"happy path: derp + same-LAN + no backoff → trial", true, modeDERP, true, past, true},
		{"disabled → never", false, modeDERP, true, past, false},
		{"no same-LAN candidate → no trial", true, modeDERP, false, past, false},
		{"already lan-trial → no re-trial", true, modeLANTrial, true, past, false},
		{"already lan-direct → no re-trial", true, modeLANDirect, true, past, false},
		{"in backoff → suppressed", true, modeDERP, true, future, false},
		{"backoff exactly now (not before) → allowed", true, modeDERP, true, now, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldStartLANTrial(tc.enabled, tc.mode, tc.hasSameLANEp, tc.backoffUntil, now)
			if got != tc.want {
				t.Errorf("shouldStartLANTrial = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestShouldStartLANTrial_BackoffSuppressesThenExpires walks the backoff
// lifecycle: suppressed during the window, allowed once it elapses.
func TestShouldStartLANTrial_BackoffSuppressesThenExpires(t *testing.T) {
	start := time.Now()
	backoffUntil := start.Add(lanTrialBackoff)

	// During backoff: suppressed.
	if shouldStartLANTrial(true, modeDERP, true, backoffUntil, start.Add(time.Minute)) {
		t.Errorf("expected trial suppressed inside backoff window")
	}
	// After backoff elapses: allowed.
	if !shouldStartLANTrial(true, modeDERP, true, backoffUntil, backoffUntil.Add(time.Second)) {
		t.Errorf("expected trial allowed after backoff elapsed")
	}
}

// ---------------------------------------------------------------------------
// evaluateLANTrial — trial→direct / trial→derp+backoff / pending
// ---------------------------------------------------------------------------

func TestEvaluateLANTrial(t *testing.T) {
	const fresh = 3 * time.Minute
	now := time.Now()
	deadline := now.Add(time.Second) // not yet expired in "now" checks below
	preTrial := now.Add(-10 * time.Second)

	t.Run("fresh handshake newer than snapshot → verified", func(t *testing.T) {
		lastHS := now.Add(-time.Second) // after preTrial, within freshWindow
		if got := evaluateLANTrial(lastHS, preTrial, deadline, now, fresh); got != trialVerified {
			t.Errorf("got %v, want trialVerified", got)
		}
	})

	t.Run("handshake not newer than snapshot → pending (not verified)", func(t *testing.T) {
		// last == snapshot: no NEW handshake observed yet, deadline not passed.
		if got := evaluateLANTrial(preTrial, preTrial, deadline, now, fresh); got != trialPending {
			t.Errorf("got %v, want trialPending", got)
		}
	})

	t.Run("stale historical handshake that predates snapshot → pending", func(t *testing.T) {
		older := preTrial.Add(-time.Minute)
		if got := evaluateLANTrial(older, preTrial, deadline, now, fresh); got != trialPending {
			t.Errorf("got %v, want trialPending", got)
		}
	})

	t.Run("zero last handshake, deadline not passed → pending", func(t *testing.T) {
		if got := evaluateLANTrial(time.Time{}, time.Time{}, deadline, now, fresh); got != trialPending {
			t.Errorf("got %v, want trialPending", got)
		}
	})

	t.Run("deadline passed, no fresh handshake → timed out", func(t *testing.T) {
		passed := now.Add(-time.Second) // deadline already in the past
		if got := evaluateLANTrial(time.Time{}, time.Time{}, passed, now, fresh); got != trialTimedOut {
			t.Errorf("got %v, want trialTimedOut", got)
		}
	})

	t.Run("deadline passed but a fresh newer handshake landed → verified wins", func(t *testing.T) {
		// Verification takes precedence over timeout: if a fresh handshake is
		// present we promote even if the deadline tick coincided.
		passed := now.Add(-time.Second)
		lastHS := now.Add(-500 * time.Millisecond)
		if got := evaluateLANTrial(lastHS, preTrial, passed, now, fresh); got != trialVerified {
			t.Errorf("got %v, want trialVerified", got)
		}
	})

	t.Run("newer-than-snapshot but STALE (outside fresh window) → not verified", func(t *testing.T) {
		// Guards the zero-snapshot edge: a non-zero but ancient handshake is
		// "after" the zero snapshot yet must not count as a verified LAN path.
		ancient := now.Add(-10 * time.Minute) // older than freshWindow
		if got := evaluateLANTrial(ancient, time.Time{}, deadline, now, fresh); got != trialPending {
			t.Errorf("got %v, want trialPending (stale handshake must not verify)", got)
		}
	})
}

// ---------------------------------------------------------------------------
// modeTracker
// ---------------------------------------------------------------------------

func TestModeTracker_DefaultsAndManaged(t *testing.T) {
	mt := newModeTracker()
	const k = "AAAA"

	// Unknown peer defaults to derp and is NOT LAN-managed (off-by-default
	// invariant: the guard must fall through to the dBind DERP check).
	if got := mt.get(k); got != modeDERP {
		t.Errorf("unknown peer mode = %v, want modeDERP", got)
	}
	if mt.isManaged(k) {
		t.Errorf("unknown/derp peer must NOT be LAN-managed (preserves off-by-default)")
	}

	// modeDERP is stored as an absence (delete) and is not LAN-managed.
	mt.set(k, modeDERP)
	if got := mt.get(k); got != modeDERP {
		t.Errorf("after set(modeDERP) get = %v, want modeDERP", got)
	}
	if mt.isManaged(k) {
		t.Errorf("modeDERP must NOT be LAN-managed")
	}

	// LAN modes are stored and ARE LAN-managed.
	for _, m := range []routingMode{modeLANTrial, modeLANDirect} {
		mt.set(k, m)
		if mt.get(k) != m {
			t.Errorf("after set(%v) get = %v", m, mt.get(k))
		}
		if !mt.isManaged(k) {
			t.Errorf("mode %v should be LAN-managed", m)
		}
	}

	// Setting back to derp removes the entry → not managed.
	mt.set(k, modeDERP)
	if mt.isManaged(k) {
		t.Errorf("after set back to derp, must not be LAN-managed")
	}

	// Remove drops back to the derp default.
	mt.set(k, modeLANDirect)
	mt.remove(k)
	if got := mt.get(k); got != modeDERP {
		t.Errorf("after remove, mode = %v, want modeDERP default", got)
	}
}

// TestModeTracker_EmptyWhenNeverWritten guards the off-by-default invariant
// directly: a tracker with no LAN-mode writes reports nothing as managed, so the
// raw-endpoint guard degrades to the legacy dBind-only check.
func TestModeTracker_EmptyWhenNeverWritten(t *testing.T) {
	mt := newModeTracker()
	for _, k := range []string{"peerA", "peerB", "peerC"} {
		if mt.isManaged(k) {
			t.Errorf("peer %q reported managed on a fresh tracker", k)
		}
		// Even an explicit derp write must not make it managed.
		mt.set(k, modeDERP)
		if mt.isManaged(k) {
			t.Errorf("peer %q reported managed after derp write", k)
		}
	}
}

// TestRoutingModeString keeps the human-readable labels stable (they appear in
// logs and the UI reachability mode).
func TestRoutingModeString(t *testing.T) {
	cases := map[routingMode]string{
		modeDERP:      "derp",
		modeLANTrial:  "lan-trial",
		modeLANDirect: "lan-direct",
	}
	for m, want := range cases {
		if got := m.String(); got != want {
			t.Errorf("routingMode(%d).String() = %q, want %q", m, got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// Composed state-machine walk: derp → trial → direct, and derp → trial → derp.
// Exercises the pure helpers in sequence the way the loop drives them.
// ---------------------------------------------------------------------------

func TestStateMachine_TrialToDirect(t *testing.T) {
	const fresh = 3 * time.Minute
	now := time.Now()

	// Start: peer on derp, same-LAN candidate present, no backoff → start trial.
	if !shouldStartLANTrial(true, modeDERP, true, time.Time{}, now) {
		t.Fatalf("expected to start trial")
	}
	mode := modeLANTrial
	preTrial := now // snapshot at trial start
	deadline := now.Add(lanTrialDuration)

	// A fresh handshake lands 200ms later, after the snapshot → verified.
	verifyAt := now.Add(200 * time.Millisecond)
	lastHS := now.Add(150 * time.Millisecond)
	switch evaluateLANTrial(lastHS, preTrial, deadline, verifyAt, fresh) {
	case trialVerified:
		mode = modeLANDirect
	default:
		t.Fatalf("expected trialVerified")
	}
	if mode != modeLANDirect {
		t.Fatalf("final mode = %v, want modeLANDirect", mode)
	}
}

func TestStateMachine_TrialToDERPBackoffSuppressesRetrial(t *testing.T) {
	const fresh = 3 * time.Minute
	now := time.Now()

	// Start trial.
	if !shouldStartLANTrial(true, modeDERP, true, time.Time{}, now) {
		t.Fatalf("expected to start trial")
	}
	preTrial := now
	deadline := now.Add(lanTrialDuration)

	// No handshake arrives; deadline passes → timeout.
	verifyAt := deadline.Add(time.Second)
	if got := evaluateLANTrial(time.Time{}, preTrial, deadline, verifyAt, fresh); got != trialTimedOut {
		t.Fatalf("expected trialTimedOut, got %v", got)
	}
	// Loop rolls back to derp and sets backoff.
	mode := modeDERP
	backoffUntil := verifyAt.Add(lanTrialBackoff)

	// Immediately after rollback, a re-trial must be suppressed by backoff.
	if shouldStartLANTrial(true, mode, true, backoffUntil, verifyAt.Add(time.Minute)) {
		t.Errorf("expected re-trial suppressed during backoff")
	}
	// Once backoff elapses, a re-trial is allowed again.
	if !shouldStartLANTrial(true, mode, true, backoffUntil, backoffUntil.Add(time.Second)) {
		t.Errorf("expected re-trial allowed after backoff")
	}
}
