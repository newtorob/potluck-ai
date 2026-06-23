//go:build windows

package tunnel

import (
	"fmt"
	"log/slog"
	"net"
	"os/exec"
	"strings"
)

// addPeerHostRoute installs a /32 host route for the peer's mesh IP via the
// Wintun interface so Windows actually delivers packets to WireGuard. Without
// it, the only route covering the household subnet is the local node's own /32
// (assigned in assignInterfaceAddress); packets to every other peer fall through
// to the default gateway and get dropped.
//
// This was a no-op stub until 2026-06-23, when a real 3-node mesh (Windows +
// Linux + macOS) showed every peer unreachable from Windows — control plane
// enrolled and "reachable", but TCP/ICMP to peer mesh IPs failed — until these
// /32 routes were added by hand. Wintun does NOT auto-populate per-peer kernel
// routes, so we install them explicitly, same as Linux/macOS.
//
// We use `New-NetRoute` (PowerShell / MSFT_NetRoute) rather than `netsh`: the
// Wintun adapter registers at the NetAdapter layer that New-NetRoute drives, but
// does NOT appear in `netsh interface ipv4 show interfaces`, so netsh route
// commands fail with "Element not found". `New-NetRoute -InterfaceAlias <name>`
// is the form that was verified working on the live mesh.
//
// PolicyStore defaults to ActiveStore (non-persistent) — the sidecar re-adds the
// route on each start, matching the ephemeral `ip route add` / `route add`
// behaviour on Linux/macOS. Idempotent: we skip the add if the route exists.
func addPeerHostRoute(ifName, peerIP string) error {
	if net.ParseIP(peerIP) == nil { // guard: peerIP is interpolated into a PS command
		return fmt.Errorf("addPeerHostRoute: invalid peer IP %q", peerIP)
	}
	ps := fmt.Sprintf(
		"$ErrorActionPreference='Stop'; "+
			"if (-not (Get-NetRoute -DestinationPrefix '%[1]s/32' -InterfaceAlias '%[2]s' -ErrorAction SilentlyContinue)) "+
			"{ New-NetRoute -DestinationPrefix '%[1]s/32' -InterfaceAlias '%[2]s' -NextHop 0.0.0.0 | Out-Null }",
		peerIP, ifName,
	)
	out, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps).CombinedOutput()
	if err != nil {
		return fmt.Errorf("add host route %s via %s: %w: %s", peerIP, ifName, err, strings.TrimSpace(string(out)))
	}
	slog.Info("tunnel: peer host route installed",
		"interface", ifName,
		"peer_ip", peerIP,
	)
	return nil
}

// removePeerHostRoute removes the /32 host route installed by addPeerHostRoute.
// Called when a peer leaves the household so its mesh IP no longer black-holes
// packets. Idempotent: -ErrorAction SilentlyContinue makes a missing route a
// no-op ("No matching MSFT_NetRoute objects found").
func removePeerHostRoute(ifName, peerIP string) error {
	if net.ParseIP(peerIP) == nil {
		return fmt.Errorf("removePeerHostRoute: invalid peer IP %q", peerIP)
	}
	ps := fmt.Sprintf(
		"Remove-NetRoute -DestinationPrefix '%s/32' -InterfaceAlias '%s' -Confirm:$false -ErrorAction SilentlyContinue",
		peerIP, ifName,
	)
	out, err := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps).CombinedOutput()
	if err != nil {
		return fmt.Errorf("remove host route %s via %s: %w: %s", peerIP, ifName, err, strings.TrimSpace(string(out)))
	}
	return nil
}
