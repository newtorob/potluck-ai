//go:build linux

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
)

// allowMeshInbound ensures a Linux mesh node is reachable for actual data, not
// just the WireGuard control channel. It opens two things, mirroring
// firewall_windows.go's rationale:
//
//  1. The mesh TUN interface (potluck0). Every packet that arrives on it has
//     already been authenticated + decrypted by WireGuard, so the interface
//     itself is the trust boundary - no value in enumerating ports.
//  2. The WG transport port (UDP wgPort) inbound on the physical NIC, so an
//     encrypted handshake/data packet from a direct LAN/WAN peer with no prior
//     conntrack state isn't dropped before it reaches wireguard-go's socket.
//     This is what lets a same-LAN peer connect direct instead of falling back
//     to the DERP relay - without it, inbound WG on the wire is dropped and the
//     only path that survives is the node's own outbound-initiated DERP
//     connection (reachable=true but ~140ms relayed instead of <5ms
//     LAN-direct).
//
// Why direct iptables rather than ufw: a stock
// Ubuntu/Pop!_OS box commonly has a default-deny INPUT policy (`:INPUT DROP`),
// sometimes with ufw *disabled* (the policy is left behind, or set by another
// manager). `ufw allow` only takes effect while ufw is active and its chains
// are traversed before the drop, so it does not reliably
// override the drop on such a box (ping stays at 100% loss with the ufw rule
// present). Inserting an ACCEPT at the top of the kernel INPUT chain is the
// one thing that always wins, regardless of ufw/firewalld state. On modern
// distros `iptables` is the iptables-nft shim, so this programs nftables under
// the hood; we also touch ip6tables for v6 transport. The ufw calls are kept
// as a best-effort secondary so a purely-ufw-managed host also records the
// intent, but correctness does not depend on them.
//
// Idempotent: each rule is `-C`-checked before `-I`, so re-running on every
// mesh bring-up (which is also what makes it survive a reboot - the daemon
// re-applies on start) never stacks duplicates.
//
// Requires root, which the sidecar already has on Linux (creating potluck0
// needs it - see the systemd unit's lack of User=).
func allowMeshInbound(ifName string, wgPort int) error {
	portSpec := fmt.Sprintf("%d", wgPort)

	// The two INPUT accepts, as argument vectors (after the "-C INPUT" /
	// "-I INPUT 1" prefix). Order: interface accept first, then WG port.
	rules := [][]string{
		{"-i", ifName, "-j", "ACCEPT"},
		{"-p", "udp", "--dport", portSpec, "-j", "ACCEPT"},
	}

	var firstErr error
	applied := 0
	// v4 is the one that matters (mesh IPs are 100.64.0.0/10 v4); v6 covers
	// a v6 WG transport and is non-fatal if absent.
	for _, bin := range []string{"iptables", "ip6tables"} {
		path, err := exec.LookPath(bin)
		if err != nil {
			if bin == "iptables" {
				// No iptables at all is genuinely unusual on Linux; surface it
				// so the fall-through to ufw-only (below) is at least logged.
				slog.Warn("tunnel: iptables not found; mesh inbound relies on ufw only",
					"interface", ifName, "wg_port", wgPort,
				)
			}
			continue
		}
		for _, rule := range rules {
			if ensureIptablesInputAccept(path, rule) {
				applied++
			} else if firstErr == nil {
				firstErr = fmt.Errorf("allowMeshInbound: %s %v: rule could not be applied", bin, rule)
			}
		}
	}

	// Best-effort ufw mirror (secondary; correctness does not depend on it).
	if _, err := exec.LookPath("ufw"); err == nil {
		_ = exec.Command("ufw", "allow", "in", "on", ifName).Run()
		_ = exec.Command("ufw", "allow", "in", fmt.Sprintf("%d/udp", wgPort)).Run()
	}

	if applied > 0 {
		slog.Info("tunnel: mesh inbound iptables rules ensured",
			"interface", ifName, "wg_port", wgPort, "rules_present", applied,
		)
		return nil
	}
	if firstErr != nil {
		return firstErr
	}
	// Nothing to do (no iptables/ip6tables present) - not an error.
	return nil
}

// ensureIptablesInputAccept checks whether an INPUT ACCEPT rule already exists
// (`iptables -C INPUT <rule>`) and inserts it at the top (`-I INPUT 1 <rule>`)
// if not. Returns true if the rule is present afterward. Best-effort: a failed
// insert is logged, not fatal, so one family/rule failing doesn't abort the
// rest.
func ensureIptablesInputAccept(path string, rule []string) bool {
	check := append([]string{"-C", "INPUT"}, rule...)
	if err := exec.Command(path, check...).Run(); err == nil {
		return true // already present
	}
	ins := append([]string{"-I", "INPUT", "1"}, rule...)
	if out, err := exec.Command(path, ins...).CombinedOutput(); err != nil {
		slog.Warn("tunnel: failed to insert mesh INPUT accept",
			"iptables", path, "rule", strings.Join(rule, " "),
			"error", err, "out", strings.TrimSpace(string(out)),
		)
		return false
	}
	return true
}

// removeMeshInbound deletes the rules added by allowMeshInbound. Best-effort:
// a missing rule on teardown is never an error (matches firewall_windows.go).
func removeMeshInbound(ifName string, wgPort int) error {
	portSpec := fmt.Sprintf("%d", wgPort)
	rules := [][]string{
		{"-i", ifName, "-j", "ACCEPT"},
		{"-p", "udp", "--dport", portSpec, "-j", "ACCEPT"},
	}
	for _, bin := range []string{"iptables", "ip6tables"} {
		path, err := exec.LookPath(bin)
		if err != nil {
			continue
		}
		for _, rule := range rules {
			// Delete every copy (in case an older build stacked duplicates).
			for i := 0; i < 8; i++ {
				del := append([]string{"-D", "INPUT"}, rule...)
				if err := exec.Command(path, del...).Run(); err != nil {
					break
				}
			}
		}
	}
	if _, err := exec.LookPath("ufw"); err == nil {
		_ = exec.Command("ufw", "delete", "allow", "in", "on", ifName).Run()
		_ = exec.Command("ufw", "delete", "allow", "in", fmt.Sprintf("%d/udp", wgPort)).Run()
	}
	return nil
}
