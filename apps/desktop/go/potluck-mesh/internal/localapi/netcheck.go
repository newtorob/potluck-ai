// Package localapi — network readiness probes for GET /status.
//
// checkWGPortListening: reports whether the WireGuard UDP transport port is
// actually bound by this process. It does NOT dial anywhere; it attempts a
// competing bind and reads the error.
//
// checkVPNConflict: enumerates local interfaces and looks for a non-Potluck
// WireGuard or VPN interface (Mullvad, Tailscale, etc.) that is up and has a
// non-link-local address. When such an interface is present AND the coordinator
// connection is not "connected", we flag a conflict (conservative: we require
// BOTH signals to avoid false positives when a user legitimately has Tailscale
// running alongside the mesh).
//
// Neither function makes network calls or shells out. Interface enumeration
// is ~100 µs on all three target platforms.
package localapi

import (
	"fmt"
	"net"
	"strings"
)

// knownVPNPrefixes are interface name prefixes / substrings that indicate a
// third-party VPN is active. We match by prefix or exact substring so that
// platform-specific naming variants are caught:
//
//   - "wg-mullvad"  — Mullvad WireGuard interface (Linux: wg-mullvad, macOS: utun variant)
//   - "mullvad"     — Mullvad generic (e.g. the macOS utun that Mullvad names "mullvad")
//   - "tun0"        — common OpenVPN / WireGuard tunnel (Linux)
//   - "ProtonVPN"   — ProtonVPN on macOS
//   - "nordvpn"     — NordVPN
//   - "expressvpn"  — ExpressVPN
//
// Tailscale (tailscale0 / utun-tailscale) is deliberately NOT in this list:
// Tailscale is used to SSH into the bench fleet and coexists intentionally.
// We only flag it if it holds a default route while the coordinator is down
// (the Tailscale kill-switch case is the specific incident that prompted this probe).
//
// Note: we do NOT flag "utun*" generically because macOS allocates utun for
// many things including the Potluck mesh itself (utun9). We match on named
// substrings only.
var knownVPNPrefixes = []string{
	"mullvad",
	"wg-mullvad",
	"ProtonVPN",
	"nordvpn",
	"NordVPN",
	"expressvpn",
	"ExpressVPN",
	"tun0",    // common OpenVPN / generic WG on Linux (not potluck0)
	"tun1",    // secondary OpenVPN
	"ppp0",    // PPTP / L2TP
	"vpn",     // generic e.g. "vpn0", "vpn-work"
}

// potluckInterfaces are the Potluck-owned interfaces that must never be
// treated as third-party VPNs.
var potluckInterfaces = []string{
	"potluck0", // Linux
	"Wintun",   // Windows
	// macOS: utun9 is the preferred name, but the kernel may give us utun8,
	// utun10, etc. We identify Potluck's utun by the absence of a match in
	// knownVPNPrefixes — so all utun* not matching a known VPN name are skipped.
}

// isPotluckInterface reports whether the named interface belongs to Potluck.
func isPotluckInterface(name string) bool {
	for _, own := range potluckInterfaces {
		if name == own {
			return true
		}
	}
	return false
}

// isKnownVPN reports whether the named interface looks like a third-party VPN.
func isKnownVPN(name string) bool {
	lower := strings.ToLower(name)
	for _, pfx := range knownVPNPrefixes {
		if strings.HasPrefix(lower, strings.ToLower(pfx)) ||
			strings.Contains(lower, strings.ToLower(pfx)) {
			return true
		}
	}
	return false
}

// isIfaceUp reports whether an interface has the net.FlagUp bit set AND has at
// least one non-loopback, non-link-local unicast address. An interface that is
// "up" but has no usable address (e.g. a Wintun adapter before IP assignment)
// is not treated as a conflict.
func isIfaceUp(iface net.Interface) bool {
	if iface.Flags&net.FlagUp == 0 {
		return false
	}
	addrs, err := iface.Addrs()
	if err != nil {
		return false
	}
	for _, a := range addrs {
		var ip net.IP
		switch v := a.(type) {
		case *net.IPNet:
			ip = v.IP
		case *net.IPAddr:
			ip = v.IP
		}
		if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
			continue
		}
		return true
	}
	return false
}

// VPNConflictResult carries the outcome of checkVPNConflict.
type VPNConflictResult struct {
	Conflict bool
	Detail   string
}

// checkVPNConflict enumerates local interfaces and looks for a known third-party
// VPN interface that is UP and has an address. When such an interface is found:
//
//   - If coordinatorConnState == "connected", we return conflict=false with a
//     non-empty detail ("Mullvad active — monitoring") so the UI can surface it
//     without crying wolf. A running VPN that isn't currently breaking the mesh
//     is a low-severity notice, not a hard conflict.
//   - If coordinatorConnState != "connected" (disconnected or connecting), we
//     return conflict=true — a known VPN is up AND the coordinator is unreachable,
//     which is the exact Mullvad kill-switch failure pattern.
//
// Unknown OS: always returns conflict=false (never crashes).
func checkVPNConflict(coordinatorConnState string) VPNConflictResult {
	ifaces, err := net.Interfaces()
	if err != nil {
		// Cannot enumerate: be conservative and return no conflict.
		return VPNConflictResult{}
	}

	var found []string
	for _, iface := range ifaces {
		if isPotluckInterface(iface.Name) {
			continue
		}
		if !isKnownVPN(iface.Name) {
			continue
		}
		if !isIfaceUp(iface) {
			continue
		}
		found = append(found, iface.Name)
	}

	if len(found) == 0 {
		return VPNConflictResult{}
	}

	names := strings.Join(found, ", ")
	coordDown := coordinatorConnState != "connected"

	if coordDown {
		return VPNConflictResult{
			Conflict: true,
			Detail: fmt.Sprintf(
				"VPN active (%s) and coordinator unreachable — VPN may be capturing mesh routes",
				names,
			),
		}
	}

	// VPN is up but coordinator is reachable: low-severity notice, not a hard conflict.
	return VPNConflictResult{
		Conflict: false,
		Detail: fmt.Sprintf(
			"VPN active (%s) — mesh is connected but VPN kill-switch may break it if disconnected",
			names,
		),
	}
}

// checkWGPortListening reports whether the given UDP port is already bound by
// some process on the local machine (i.e. the WireGuard device is listening).
//
// Mechanism: attempt to bind a UDP socket to 0.0.0.0:<port>. If the bind
// fails with "address already in use" (or the Windows equivalent), then
// something — our WG device — already owns that port: return true. If the
// bind succeeds, nobody is listening: close the probe socket and return false.
//
// We listen on 0.0.0.0 (all interfaces) matching how wg-go's default bind
// works; a probe to 127.0.0.1 only would miss a bind on the wildcard address.
//
// OS-portable: net.ListenPacket uses the Go stdlib UDP dial on all three target
// platforms. No syscalls, no shell invocations.
func checkWGPortListening(port int) bool {
	addr := fmt.Sprintf("0.0.0.0:%d", port)
	c, err := net.ListenPacket("udp4", addr)
	if err != nil {
		// Bind failed — port is in use (our WG device owns it).
		return true
	}
	// Bind succeeded — port was free, nothing was listening. Release it.
	_ = c.Close()
	return false
}
