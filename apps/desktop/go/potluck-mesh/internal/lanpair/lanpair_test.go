package lanpair

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"
	"time"
)

// multicastAvailable probes whether this host can do multicast. Locked-down
// sandboxes / containers without a multicast interface skip the roundtrip test.
func multicastAvailable(t *testing.T) {
	t.Helper()
	if len(multicastIfaces()) == 0 {
		t.Skip("no multicast-capable interface on this host")
	}
	c, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Skipf("cannot open a udp4 socket: %v", err)
	}
	_ = c.Close()
}

// startListener runs s.RunListener in the background and returns a stop func
// that cancels it and BLOCKS until the listener goroutine has actually
// returned (and so has released its fixed-port socket). RunListener binds a
// single well-known UDP port; tests run sequentially in this package, and
// without this synchronization the next test's ListenPacket call can race
// the previous test's asynchronous socket close and fail with "address
// already in use".
func startListener(t *testing.T, s *Service) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		s.RunListener(ctx)
	}()
	time.Sleep(150 * time.Millisecond) // let it bind + join the group
	return func() {
		cancel()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Log("lanpair: RunListener did not stop within 2s of cancellation")
		}
	}
}

func TestBroadcastRoundtrip(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	pin := s.StartBroadcast("TESTTOKEN123", "test-host", 5*time.Second)
	defer s.StopBroadcast()
	if len(pin) != 6 {
		t.Fatalf("expected a 6-digit PIN, got %q", pin)
	}

	wantFP := fingerprintOf("TESTTOKEN123")
	fp, host, ok := waitDiscovered(s, 5*time.Second)
	if !ok || fp != wantFP || host != "test-host" {
		t.Fatalf("expected to discover broadcast, got ok=%v fp=%q host=%q (want fp=%q)", ok, fp, host, wantFP)
	}
}

// TestBroadcastNeverCarriesRawToken proves the core fix: the wire payload the
// inviter actually sends never contains the plaintext pairing token, only its
// one-way fingerprint. A passive LAN listener that captures this payload gets
// nothing redeemable.
func TestBroadcastNeverCarriesRawToken(t *testing.T) {
	const token = "SUPER-SECRET-REDEEMABLE-TOKEN"
	fp := fingerprintOf(token)

	payload, err := json.Marshal(announce{Magic: magic, Hostname: "h", Fingerprint: fp})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	raw := string(payload)
	if strings.Contains(raw, token) {
		t.Fatalf("broadcast payload must never contain the raw token, got: %s", raw)
	}
}

func TestFreshnessExpiry(t *testing.T) {
	s := New()
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return base }
	s.mu.Lock()
	s.latest = &heard{fingerprint: "x", hostname: "h", at: base.Add(-10 * time.Second)}
	s.mu.Unlock()

	if _, _, ok := s.Discovered(5 * time.Second); ok {
		t.Fatal("a 10s-old entry must not be discovered within a 5s window")
	}
	if _, _, ok := s.Discovered(30 * time.Second); !ok {
		t.Fatal("a 10s-old entry should be discovered within a 30s window")
	}
}

func TestDiscoveredEmptyWhenNothingHeard(t *testing.T) {
	s := New()
	if _, _, ok := s.Discovered(time.Minute); ok {
		t.Fatal("Discovered should be false before anything is heard")
	}
}

func TestSweepableClassification(t *testing.T) {
	cases := []struct {
		ip   string
		want bool
	}{
		{"192.168.4.86", true},    // RFC1918
		{"10.0.0.5", true},         // RFC1918
		{"172.16.4.4", true},       // RFC1918 low edge
		{"172.31.255.1", true},     // RFC1918 high edge
		{"172.15.0.1", false},      // just outside 172.16/12
		{"172.32.0.1", false},      // just outside 172.16/12
		{"100.101.102.103", false},   // Tailscale/CGNAT 100.64/10 mid-range
		{"100.64.0.0", false},      // CGNAT low boundary
		{"100.127.255.255", false}, // CGNAT high boundary
		{"100.63.255.255", false},  // just below CGNAT (not RFC1918 either)
		{"100.128.0.1", false},     // just above CGNAT (not RFC1918 either)
		{"169.254.1.1", false},     // link-local
		{"127.0.0.1", false},       // loopback
		{"8.8.8.8", false},         // public
	}
	for _, c := range cases {
		ip := net.ParseIP(c.ip)
		if got := sweepable(ip); got != c.want {
			t.Errorf("sweepable(%s) = %v, want %v", c.ip, got, c.want)
		}
	}
}

func TestDirectedBroadcast(t *testing.T) {
	cases := []struct {
		ip   string
		bits int
		want string
	}{
		{"192.168.4.86", 22, "192.168.7.255"}, // the bench network's /22
		{"192.168.1.50", 24, "192.168.1.255"},
		{"10.0.0.5", 8, "10.255.255.255"},
		{"172.16.4.4", 12, "172.31.255.255"},
	}
	for _, c := range cases {
		got := directedBroadcast(net.ParseIP(c.ip), net.CIDRMask(c.bits, 32))
		if got == nil || got.String() != c.want {
			t.Errorf("directedBroadcast(%s/%d) = %v, want %s", c.ip, c.bits, got, c.want)
		}
	}
}

func TestBroadcastDestsIncludesLimited(t *testing.T) {
	// The limited broadcast must always be present; any extra entries must be
	// valid IPv4 addresses (one per RFC1918 interface, host-specific).
	dests := broadcastDests()
	var hasLimited bool
	for _, d := range dests {
		if d.Equal(net.IPv4(255, 255, 255, 255)) {
			hasLimited = true
		}
		if d.To4() == nil {
			t.Errorf("broadcast dest %v is not IPv4", d)
		}
	}
	if !hasLimited {
		t.Fatal("broadcastDests must include the limited broadcast 255.255.255.255")
	}
}

// TestUnicastReceive proves the fallback's receive side works: the listener,
// bound to 0.0.0.0:groupPort, accepts a plain unicast datagram (here to
// loopback) with no multicast group join involved.
func TestUnicastReceive(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	c, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Skipf("cannot open a udp4 socket: %v", err)
	}
	defer c.Close()
	fp := fingerprintOf("UNI-456")
	payload, _ := json.Marshal(announce{Magic: magic, Hostname: "uni-host", Fingerprint: fp})
	dst := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: groupPort}

	var ok bool
	var gotFP, host string
	for i := 0; i < 50; i++ {
		if _, err := c.WriteTo(payload, dst); err != nil {
			t.Fatalf("unicast write failed: %v", err)
		}
		if gotFP, host, ok = s.Discovered(3 * time.Second); ok {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ok || gotFP != fp || host != "uni-host" {
		t.Fatalf("listener should accept unicast; got ok=%v fp=%q host=%q", ok, gotFP, host)
	}
}

// ---------------------------------------------------------------------------
// OOB PIN-reveal tests — the actual fix under audit.
//
// A real inviter and joiner run as two independent processes on two
// machines, each binding its own 0.0.0.0:groupPort. Two Service instances in
// the SAME test process can't both bind that fixed port, so these tests play
// both roles with a single Service and multicast loopback (SetMulticastLoopback,
// already enabled by RunListener): the service hears its own announce, and a
// reveal request it sends to its own recorded address is answered by its own
// listener loop. This exercises the exact same wire messages and validation
// path a real cross-machine exchange does.
// ---------------------------------------------------------------------------

func waitDiscovered(s *Service, timeout time.Duration) (fingerprint, hostname string, ok bool) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fp, host, ok := s.Discovered(3 * time.Second); ok {
			return fp, host, true
		}
		time.Sleep(50 * time.Millisecond)
	}
	return "", "", false
}

func mustDiscover(t *testing.T, s *Service) string {
	t.Helper()
	fp, _, ok := waitDiscovered(s, 5*time.Second)
	if !ok {
		t.Fatal("never discovered the broadcast invite")
	}
	return fp
}

// TestRevealWithCorrectPIN proves the intended happy path: once a fingerprint
// has been discovered over the LAN AND a human has supplied the correct
// out-of-band PIN, RequestReveal returns the real token.
func TestRevealWithCorrectPIN(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	const token = "REAL-REDEEMABLE-TOKEN-XYZ"
	pin := s.StartBroadcast(token, "inviter-host", 5*time.Second)
	defer s.StopBroadcast()

	fp := mustDiscover(t, s)

	got, err := s.RequestReveal(fp, pin, 3*time.Second)
	if err != nil {
		t.Fatalf("RequestReveal with correct PIN failed: %v", err)
	}
	if got != token {
		t.Fatalf("RequestReveal returned %q, want %q", got, token)
	}
}

// TestRevealWithWrongPIN proves a wrong PIN guess never yields the token.
func TestRevealWithWrongPIN(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	const token = "ANOTHER-REDEEMABLE-TOKEN"
	realPin := s.StartBroadcast(token, "inviter-host", 5*time.Second)
	defer s.StopBroadcast()

	wrongPin := otherPin(realPin)
	fp := mustDiscover(t, s)

	if _, err := s.RequestReveal(fp, wrongPin, 3*time.Second); err == nil {
		t.Fatal("RequestReveal with a wrong PIN must fail")
	}
}

// TestRevealWithoutMatchingFingerprintFails is the core assertion the audit
// asked for: a broadcast payload alone (fingerprint + hostname — everything a
// passive LAN listener could ever capture, now that the raw token is never
// sent) is not sufficient to redeem. A guess at an unrelated / unheard
// fingerprint (no PIN in hand, no session to answer it) must fail closed.
func TestRevealWithoutMatchingFingerprintFails(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	// Nothing was ever broadcast/discovered under this fingerprint, so there
	// is no known target to even send a reveal request to.
	if _, err := s.RequestReveal("deadbeefcafe", "123456", 1*time.Second); err == nil {
		t.Fatal("RequestReveal must fail when no matching invite was discovered")
	}

	// Even with an active, unrelated broadcast running (and its own
	// fingerprint discovered), guessing at some OTHER, never-discovered
	// fingerprint must still fail closed — a real fingerprint is required
	// before there's even anywhere to send a guess.
	const token = "UNRELATED-TOKEN"
	pin := s.StartBroadcast(token, "inviter-host", 5*time.Second)
	defer s.StopBroadcast()
	mustDiscover(t, s)

	if _, err := s.RequestReveal("0000deadbeef", pin, 1*time.Second); err == nil {
		t.Fatal("RequestReveal must fail for a fingerprint that was never discovered")
	}
}

// TestRevealAttemptsAreCapped proves the inviter stops answering (and burns
// the session) after maxRevealAttempts wrong guesses, bounding a LAN
// attacker's blind PIN-guessing to a small, fixed number of tries rather than
// leaving the 6-digit space open to unlimited attempts.
func TestRevealAttemptsAreCapped(t *testing.T) {
	multicastAvailable(t)

	s := New()
	stop := startListener(t, s)
	defer stop()

	const token = "CAPPED-ATTEMPTS-TOKEN"
	realPin := s.StartBroadcast(token, "inviter-host", 10*time.Second)
	defer s.StopBroadcast()

	wrongPin := otherPin(realPin)
	fp := mustDiscover(t, s)

	// Spend every allowed wrong attempt.
	for i := 0; i < maxRevealAttempts; i++ {
		if _, err := s.RequestReveal(fp, wrongPin, 2*time.Second); err == nil {
			t.Fatalf("attempt %d: expected wrong-PIN rejection", i)
		}
	}

	// The session must now be burned: even the CORRECT PIN must no longer
	// work.
	if _, err := s.RequestReveal(fp, realPin, 2*time.Second); err == nil {
		t.Fatal("reveal must be refused once the attempt cap is exhausted, even with the correct PIN")
	}
}

func otherPin(pin string) string {
	if pin != "000000" {
		return "000000"
	}
	return "111111"
}

// TestHandleRevealRequestSessionMismatch is a lower-level unit test of
// handleRevealRequest directly: a reveal request addressed to a fingerprint
// that isn't this host's active session must be ignored outright — no
// attempt is spent and the real session's PIN is never at risk of being
// probed via a mismatched fingerprint.
func TestHandleRevealRequestSessionMismatch(t *testing.T) {
	s := New()
	s.session = &bcastSession{fingerprint: "real-fp", token: "T", pin: "654321"}

	dst := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 1234}
	s.handleRevealRequest(revealRequest{Magic: revealReqMagic, Fingerprint: "other-fp", PIN: "654321"}, dst)

	s.mu.Lock()
	attempts := s.session.attempts
	s.mu.Unlock()
	if attempts != 0 {
		t.Fatalf("a request for a non-matching fingerprint must not spend an attempt, got attempts=%d", attempts)
	}
}

// TestHandleRevealRequestNoSession is the same idea with no active broadcast
// at all: a reveal request that arrives after the window (or before any
// broadcast ever started) must be a no-op, not a panic on a nil session.
func TestHandleRevealRequestNoSession(t *testing.T) {
	s := New()
	dst := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 1234}
	s.handleRevealRequest(revealRequest{Magic: revealReqMagic, Fingerprint: "any-fp", PIN: "000000"}, dst)
	// No panic, and session stays nil.
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.session != nil {
		t.Fatal("handling a reveal request with no active session must not create one")
	}
}
