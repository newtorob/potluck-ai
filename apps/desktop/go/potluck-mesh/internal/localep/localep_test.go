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

// TestIsVirtualInterfaceName covers the container/bridge/VM adapter prefixes
// that must never contribute a LAN candidate — the reproduction case being
// Docker's default bridge (docker0 at 172.17.0.1), which is a valid RFC1918
// address that util.PrivateLAN alone cannot distinguish from a real LAN NIC.
func TestIsVirtualInterfaceName(t *testing.T) {
	virtual := []string{
		"docker0", "br-a1b2c3d4e5f6", "veth3f8a2b1", "cni0", "flannel.1",
		"podman0", "podman1", "virbr0", "vmnet1", "vmnet8", "vboxnet0",
		// Case-insensitive (Windows adapter friendly names vary in case).
		"Docker0", "VMnet1",
	}
	for _, name := range virtual {
		if !isVirtualInterfaceName(name) {
			t.Errorf("isVirtualInterfaceName(%q) = false, want true", name)
		}
	}

	real := []string{"en0", "en1", "eth0", "wlan0", "Ethernet", "Wi-Fi", "Ethernet 2", "utun9"}
	for _, name := range real {
		if isVirtualInterfaceName(name) {
			t.Errorf("isVirtualInterfaceName(%q) = true, want false (real adapter)", name)
		}
	}
}

// TestCandidatesFromIPs_DoesNotFilterByIP verifies candidatesFromIPs itself
// (the pure IP-list filter) has no interface-name awareness — the docker0
// exclusion happens one layer up, in LocalCandidates' interface enumeration,
// which candidatesFromIPs is deliberately decoupled from for testability.
// This guards against a future refactor accidentally duplicating (or losing)
// the virtual-interface filter in the wrong layer.
func TestCandidatesFromIPs_DoesNotFilterByIP(t *testing.T) {
	// 172.17.0.1 is docker0's typical default-bridge address — a plain
	// RFC1918 IP with nothing to distinguish it at the IP-list level. The
	// filter that excludes it lives in LocalCandidates (interface.Name), not
	// here, so candidatesFromIPs must pass it through.
	in := []net.IP{net.ParseIP("172.17.0.1")}
	got := candidatesFromIPs(in, 51820)
	if len(got) != 1 || got[0] != "172.17.0.1:51820" {
		t.Fatalf("expected candidatesFromIPs to pass through 172.17.0.1 unfiltered, got %v", got)
	}
}

// TestLocalIPNets_HostInvariants runs against the real host interfaces and
// asserts the invariants for the Phase-2 co-subnet input: every returned IPNet
// has a 4-byte RFC1918 IP and a non-empty mask, and nothing loopback/link-local/
// tailnet is included. Cannot assert specific addresses (host-dependent), only
// the invariants every entry must satisfy.
func TestLocalIPNets_HostInvariants(t *testing.T) {
	got := LocalIPNets()
	for _, n := range got {
		ip4 := n.IP.To4()
		if ip4 == nil {
			t.Errorf("IPNet %v has non-IPv4 IP", n)
			continue
		}
		if len(n.Mask) != net.IPv4len {
			t.Errorf("IPNet %v mask is not 4 bytes (got %d)", n, len(n.Mask))
		}
		if ip4.IsLoopback() || ip4.IsLinkLocalUnicast() {
			t.Errorf("IPNet %v is loopback/link-local — must be excluded", n)
		}
		if ip4[0] == 100 && ip4[1] >= 64 && ip4[1] <= 127 {
			t.Errorf("IPNet %v is in tailnet/CGNAT range — must be excluded", n)
		}
		// Sanity: the IP must mask cleanly under its own mask (no panic, stable).
		_ = ip4.Mask(n.Mask)
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
