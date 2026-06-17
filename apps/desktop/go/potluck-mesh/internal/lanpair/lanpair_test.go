package lanpair

import (
	"context"
	"encoding/json"
	"net"
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

func TestBroadcastRoundtrip(t *testing.T) {
	multicastAvailable(t)

	s := New()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.RunListener(ctx)
	time.Sleep(150 * time.Millisecond) // let the listener join the group

	s.StartBroadcast("TESTTOKEN123", "test-host", 5*time.Second)
	defer s.StopBroadcast()

	var tok, host string
	var ok bool
	for i := 0; i < 50; i++ {
		if tok, host, ok = s.Discovered(3 * time.Second); ok {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ok || tok != "TESTTOKEN123" || host != "test-host" {
		t.Fatalf("expected to discover broadcast, got ok=%v tok=%q host=%q", ok, tok, host)
	}
}

func TestFreshnessExpiry(t *testing.T) {
	s := New()
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return base }
	s.mu.Lock()
	s.latest = &heard{token: "x", hostname: "h", at: base.Add(-10 * time.Second)}
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
		{"192.168.4.86", true},  // RFC1918
		{"10.0.0.5", true},       // RFC1918
		{"172.16.4.4", true},     // RFC1918 low edge
		{"172.31.255.1", true},   // RFC1918 high edge
		{"172.15.0.1", false},     // just outside 172.16/12
		{"172.32.0.1", false},     // just outside 172.16/12
		{"100.101.102.103", false},  // Tailscale/CGNAT 100.64/10 mid-range
		{"100.64.0.0", false},     // CGNAT low boundary
		{"100.127.255.255", false},// CGNAT high boundary
		{"100.63.255.255", false}, // just below CGNAT (not RFC1918 either)
		{"100.128.0.1", false},    // just above CGNAT (not RFC1918 either)
		{"169.254.1.1", false},    // link-local
		{"127.0.0.1", false},     // loopback
		{"8.8.8.8", false},       // public
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
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go s.RunListener(ctx)
	time.Sleep(150 * time.Millisecond)

	c, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Skipf("cannot open a udp4 socket: %v", err)
	}
	defer c.Close()
	payload, _ := json.Marshal(announce{Magic: magic, Token: "UNI-456", Hostname: "uni-host"})
	dst := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: groupPort}

	var ok bool
	var tok, host string
	for i := 0; i < 50; i++ {
		if _, err := c.WriteTo(payload, dst); err != nil {
			t.Fatalf("unicast write failed: %v", err)
		}
		if tok, host, ok = s.Discovered(3 * time.Second); ok {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !ok || tok != "UNI-456" || host != "uni-host" {
		t.Fatalf("listener should accept unicast; got ok=%v tok=%q host=%q", ok, tok, host)
	}
}
