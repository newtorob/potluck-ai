//go:build windows

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
)

// firewallRuleName is the stable DisplayName for the mesh inbound-allow rule, so
// the add is idempotent and the remove can find it again.
func firewallRuleName(ifName string) string {
	return fmt.Sprintf("Potluck Mesh inbound (%s)", ifName)
}

// allowMeshInbound ensures a Windows Firewall rule permitting inbound traffic on
// the mesh (Wintun) interface.
//
// Why this exists: a freshly-created Wintun adapter lands on a firewall profile
// that blocks inbound, so without this rule a Windows node is reachable by
// nobody on the mesh. Contributor mode, pipeline stages, and peer inference all
// silently fail with no signal to the user -- they did everything right and the
// node still "doesn't work." Tailscale opens its own interface the same way on
// install; a member-operated mesh has to be this easy on Windows too.
//
// Scope: the rule is bound to the mesh INTERFACE, not a port range. Every packet
// that arrives on Wintun has already been authenticated + decrypted by
// WireGuard, so the interface itself is the trust boundary -- there is no value
// in also enumerating ports. PolicyStore=ActiveStore keeps the rule ephemeral
// (gone on reboot); the sidecar re-adds it on every mesh bring-up, so we never
// leave a persistent firewall change on the user's machine. Idempotent.
//
// Requires elevation, which the sidecar already has on Windows (creating the
// Wintun TUN adapter needs admin).
func allowMeshInbound(ifName string) error {
	rule := firewallRuleName(ifName)
	ps := fmt.Sprintf(
		"$ErrorActionPreference='Stop'; "+
			"if (-not (Get-NetFirewallRule -DisplayName '%[1]s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue)) "+
			"{ New-NetFirewallRule -DisplayName '%[1]s' -Direction Inbound -InterfaceAlias '%[2]s' "+
			"-Action Allow -PolicyStore ActiveStore | Out-Null }",
		rule, ifName,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("allowMeshInbound %q: %w: %s", ifName, err, string(out))
	}
	slog.Info("tunnel: mesh inbound firewall rule ensured", "interface", ifName, "rule", rule)
	return nil
}

// removeMeshInbound deletes the rule added by allowMeshInbound. Best-effort: a
// missing rule (already torn down, or the add never ran) is not an error.
func removeMeshInbound(ifName string) error {
	rule := firewallRuleName(ifName)
	ps := fmt.Sprintf(
		"Remove-NetFirewallRule -DisplayName '%s' -PolicyStore ActiveStore -ErrorAction SilentlyContinue",
		rule,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-NonInteractive", "-Command", ps)
	if out, err := cmd.CombinedOutput(); err != nil {
		slog.Debug("tunnel: removeMeshInbound (ignored)", "interface", ifName, "error", err, "out", string(out))
	}
	return nil
}
