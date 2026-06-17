package lanpair

import (
	"context"
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
