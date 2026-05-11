//go:build windows

package tunnel

import (
	"log/slog"
)

// assignInterfaceAddress is NOT IMPLEMENTED on Windows in week 3.
//
// Assigning an IP to a Wintun adapter requires either:
//   - P/Invoke to iphlpapi.dll (AddIPAddress / CreateUnicastIpAddressEntry)
//   - Calling `netsh interface ip add address` as a subprocess
//   - Using the Windows IP Helper API via cgo
//
// All three paths are non-trivial without cgo. The cross-compilation constraint
// (CGO_ENABLED=0 for Windows cross-builds from macOS/Linux) rules out cgo in CI.
//
// Impact: The WireGuard session initialises correctly; the sidecar can still
// receive WG handshakes from peers that have the correct AllowedIPs configured.
// However, the Windows node's mesh_ip is NOT routable on the local system —
// traffic to 100.64.x.x will not traverse the tunnel on Windows until this is
// implemented.
//
// TODO(week4-windows): implement via CreateUnicastIpAddressEntry from
// iphlpapi.dll using a minimal CGO shim, or via `netsh` subprocess (simpler,
// no cgo, but requires netsh in PATH which is true on all Windows versions).
//
// The netsh approach:
//   netsh interface ip add address "Wintun" <ip> 255.255.255.255
// This does not require elevation if the interface was created by our process.
func assignInterfaceAddress(ifName, ip string) error {
	slog.Warn("tunnel: Windows mesh IP assignment not yet implemented — "+
		"mesh_ip will not be routable on this node until week 4",
		"interface", ifName,
		"mesh_ip", ip,
	)
	// Return nil (not an error) so the rest of startup proceeds normally.
	// The WG device is still useful: peers with our pubkey can configure us
	// as a peer and reach us if we initiate. The limitation is inbound-only
	// routing from this Windows node's perspective.
	return nil
}
