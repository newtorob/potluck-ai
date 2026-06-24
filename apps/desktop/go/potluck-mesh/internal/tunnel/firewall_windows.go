//go:build windows

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
)

// firewallRuleName is the stable DisplayName for the mesh inbound-allow rule
// bound to the Wintun INTERFACE, so the add is idempotent and the remove can
// find it again.
func firewallRuleName(ifName string) string {
	return fmt.Sprintf("Potluck Mesh inbound (%s)", ifName)
}

// firewallPortRuleName is the stable DisplayName for the rule that opens the WG
// TRANSPORT port inbound on the physical NIC. Keyed by port so a port change
// (unlikely) does not collide with a stale rule.
func firewallPortRuleName(wgPort int) string {
	return fmt.Sprintf("Potluck Mesh WG transport (UDP %d)", wgPort)
}

// allowMeshInbound ensures the Windows Firewall rules a mesh node needs to be
// reachable. It opens TWO things:
//
//  1. The mesh (Wintun) INTERFACE. A freshly-created Wintun adapter lands on a
//     firewall profile that blocks inbound, so without this rule a Windows node
//     is reachable by nobody on the mesh. Every packet that arrives on Wintun
//     has already been authenticated + decrypted by WireGuard, so the interface
//     itself is the trust boundary -- there is no value in enumerating ports.
//
//  2. The WG TRANSPORT port (UDP wgPort) inbound on the PHYSICAL NIC. The
//     interface rule above does nothing for the ENCRYPTED WireGuard packets that
//     a direct LAN/WAN peer sends to this node's real ip:wgPort -- those arrive
//     on the physical adapter, before Wintun, and are dropped by default. Without
//     this rule a direct LAN handshake (POTLUCK_LAN_DIRECT) can never complete to
//     a Windows node: the peer fires the handshake-init, Windows silently drops
//     it, and the trial times out. Tailscale opens its own UDP port the same way.
//
// Why this exists: contributor mode, pipeline stages, and peer inference all
// silently fail with no signal to the user otherwise -- they did everything right
// and the node still "doesn't work." A member-operated mesh has to be this easy
// on Windows too.
//
// PolicyStore=ActiveStore keeps both rules ephemeral (gone on reboot); the
// sidecar re-adds them on every mesh bring-up, so we never leave a persistent
// firewall change on the user's machine. Both adds are idempotent.
//
// Requires elevation, which the sidecar already has on Windows (creating the
// Wintun TUN adapter needs admin).
func allowMeshInbound(ifName string, wgPort int) error {
	ifRule := firewallRuleName(ifName)
	portRule := firewallPortRuleName(wgPort)
	ps := fmt.Sprintf(
		"$ErrorActionPreference='Stop'; "+
			// (1) interface rule
			"if (-not (Get-NetFirewallRule -DisplayName '%[1]s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue)) "+
			"{ New-NetFirewallRule -DisplayName '%[1]s' -Direction Inbound -InterfaceAlias '%[2]s' "+
			"-Action Allow -PolicyStore ActiveStore | Out-Null }; "+
			// (2) WG transport port rule (physical NIC)
			"if (-not (Get-NetFirewallRule -DisplayName '%[3]s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue)) "+
			"{ New-NetFirewallRule -DisplayName '%[3]s' -Direction Inbound -Protocol UDP -LocalPort %[4]d "+
			"-Action Allow -PolicyStore ActiveStore | Out-Null }",
		ifRule, ifName, portRule, wgPort,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("allowMeshInbound %q/udp%d: %w: %s", ifName, wgPort, err, string(out))
	}
	slog.Info("tunnel: mesh inbound firewall rules ensured",
		"interface", ifName, "if_rule", ifRule, "wg_port", wgPort, "port_rule", portRule)
	return nil
}

// removeMeshInbound deletes both rules added by allowMeshInbound. Best-effort: a
// missing rule (already torn down, or the add never ran) is not an error.
func removeMeshInbound(ifName string, wgPort int) error {
	ifRule := firewallRuleName(ifName)
	portRule := firewallPortRuleName(wgPort)
	ps := fmt.Sprintf(
		"Remove-NetFirewallRule -DisplayName '%s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue; "+
			"Remove-NetFirewallRule -DisplayName '%s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue",
		ifRule, portRule,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	if out, err := cmd.CombinedOutput(); err != nil {
		slog.Debug("tunnel: removeMeshInbound (ignored)", "interface", ifName, "wg_port", wgPort, "error", err, "out", string(out))
	}
	return nil
}
