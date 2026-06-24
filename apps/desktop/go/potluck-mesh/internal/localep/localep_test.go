package localep

import (
	"fmt"
	"net"
	"strings"
	"testing"
)

// TestCandidatesFromIPs_FilterFormatDedupSort exercises the core filtering
// logic with a fixed address set so it doesn't depend on the host's interfaces.
func TestCandidatesFromIPs_FilterFormatDedupSort(t *testing.T) {
	const wgPort = 51820
	in := []net.IP{
		net.ParseIP("10.0.0.5"),        // RFC1918 — keep
		net.ParseIP("192.168.1.20"),    // RFC1918 — keep
		net.ParseIP("172.16.4.4"),      // RFC1918 — keep
		net.ParseIP("172.32.0.1"),      // 172.32 is OUTSIDE 172.16/12 — drop
		net.ParseIP("100.101.102.103"), // 100.64/10 tailnet/CGNAT — drop
		net.ParseIP("127.0.0.1"),       // loopback — drop
		net.ParseIP("169.254.1.1"),     // link-local — drop
		net.ParseIP("8.8.8.8"),         // public — drop
		net.ParseIP("10.0.0.5"),        // duplicate of first — dedup
		net.ParseIP("fe80::1"),         // IPv6 link-local — drop (not IPv4)
		net.ParseIP("2606:4700::1111"), // IPv6 global — drop (not IPv4)
	}

	got := candidatesFromIPs(in, wgPort)

	want := []string{
		"10.0.0.5:51820",
		"172.16.4.4:51820",
		"192.168.1.20:51820",
	}
	if len(got) != len(want) {
		t.Fatalf("got %d candidates %v, want %d %v", len(got), got, len(want), want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("candidate[%d] = %q, want %q (full: %v)", i, got[i], want[i], got)
		}
	}

	// Sorted output: each element must be <= the next.
	for i := 1; i < len(got); i++ {
		if got[i-1] > got[i] {
			t.Errorf("output not sorted: %q before %q", got[i-1], got[i])
		}
	}
}

// TestCandidatesFromIPs_CapsLength verifies the maxCandidates cap.
func TestCandidatesFromIPs_CapsLength(t *testing.T) {
	var in []net.IP
	for i := 0; i < maxCandidates+5; i++ {
		in = append(in, net.ParseIP(fmt.Sprintf("10.0.0.%d", i+1)))
	}
	got := candidatesFromIPs(in, 51820)
	if len(got) != maxCandidates {
		t.Fatalf("expected output capped at %d, got %d (%v)", maxCandidates, len(got), got)
	}
}

// TestCandidatesFromIPs_NoTailnetEverAdvertised is the privacy-invariant guard:
// no 100.64.0.0/10 address may ever appear as a candidate.
func TestCandidatesFromIPs_NoTailnetEverAdvertised(t *testing.T) {
	in := []net.IP{
		net.ParseIP("100.64.0.1"),
		net.ParseIP("100.100.100.100"),
		net.ParseIP("100.127.255.254"),
		net.ParseIP("192.168.50.50"), // one legit RFC1918 to prove the function works
	}
	got := candidatesFromIPs(in, 51820)
	for _, ep := range got {
		if strings.HasPrefix(ep, "100.") {
			t.Errorf("tailnet/CGNAT address leaked into candidates: %q", ep)
		}
	}
	if len(got) != 1 || got[0] != "192.168.50.50:51820" {
		t.Fatalf("expected only the RFC1918 address, got %v", got)
	}
}

// TestLocalCandidates_HostInvariants runs against the real host interfaces and
// asserts the invariants hold for whatever the machine actually has: every
// entry is "<RFC1918 ip>:<wgPort>", and nothing is loopback/link-local/tailnet.
func TestLocalCandidates_HostInvariants(t *testing.T) {
	const wgPort = 51820
	got := LocalCandidates(wgPort)
	if len(got) > maxCandidates {
		t.Fatalf("LocalCandidates returned %d entries, exceeds cap %d", len(got), maxCandidates)
	}
	suffix := fmt.Sprintf(":%d", wgPort)
	for _, ep := range got {
		if !strings.HasSuffix(ep, suffix) {
			t.Errorf("candidate %q does not use wgPort %d", ep, wgPort)
		}
		host, _, err := net.SplitHostPort(ep)
		if err != nil {
			t.Errorf("candidate %q is not a valid host:port: %v", ep, err)
			continue
		}
		ip := net.ParseIP(host)
		if ip == nil {
			t.Errorf("candidate %q has unparseable IP %q", ep, host)
			continue
		}
		if ip.IsLoopback() || ip.IsLinkLocalUnicast() {
			t.Errorf("candidate %q is loopback/link-local — must be excluded", ep)
		}
		if ip4 := ip.To4(); ip4 != nil && ip4[0] == 100 && ip4[1] >= 64 && ip4[1] <= 127 {
			t.Errorf("candidate %q is in tailnet/CGNAT range — must be excluded", ep)
		}
	}
}
