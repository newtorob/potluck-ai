//go:build linux

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
)

// allowMeshInbound ensures the ufw rules a Linux mesh node needs to be
// reachable for actual DATA, not just the WireGuard control channel. Opens
// TWO things, mirroring firewall_windows.go's rationale exactly:
//
//  1. The mesh TUN INTERFACE (potluck0/wg-mesh/etc). Every packet that
//     arrives on it has already been authenticated + decrypted by WireGuard,
//     so the interface itself is the trust boundary — there is no value in
//     enumerating ports.
//  2. The WG TRANSPORT port (UDP wgPort) inbound on the PHYSICAL NIC, so an
//     encrypted handshake-init from a direct LAN/WAN peer that hasn't yet
//     seen an outbound packet from this node (no conntrack state yet) isn't
//     dropped before it ever reaches wireguard-go's own socket.
//
// Why this exists (found live against prod household 00000000, 2026-08-14 —
// see firewall_unix.go's doc for the full trace): a stock Ubuntu/Pop!_OS
// install commonly ships ufw active with a default-deny INPUT policy. ufw's
// stateful conntrack lets WireGuard's own UDP handshake/keepalive traffic
// through — it rides on the connection state created by THIS node's own
// outbound punch/keepalive — so a peer reads reachable=true with a fresh
// handshake. But the DECRYPTED IP packets WireGuard hands to the mesh TUN
// interface (ICMP, TCP SYNs, any real application traffic) are a genuinely
// NEW, unrelated flow from ufw's point of view and hit the default-deny
// INPUT policy with no matching ALLOW rule — silently dropped. The result is
// exactly what was observed: reachable=true / mode="direct" / fresh
// handshake on BOTH sides, 100% ping/TCP loss in BOTH directions.
//
// Scope: this only manages ufw (confirmed present+active on the reproduction
// machine via `systemctl is-active ufw`). If ufw is not installed, this is a
// no-op — we don't guess at firewalld/nftables/bare-iptables configuration
// on a machine that isn't using ufw; TODO(post-launch): cover those the same
// way if a fleet member reports the same symptom without ufw involved.
//
// Idempotent: `ufw allow` on an already-present rule exits 0 ("Skipping
// adding existing rule"), so re-running this on every mesh bring-up (same
// spirit as Windows' ActiveStore rules) is always safe.
//
// Requires root, which the sidecar already has on Linux (creating the potluck0
// TUN device needs it — see the systemd unit's lack of User=).
func allowMeshInbound(ifName string, wgPort int) error {
	if _, err := exec.LookPath("ufw"); err != nil {
		slog.Debug("tunnel: ufw not installed, skipping mesh inbound firewall rules",
			"interface", ifName, "wg_port", wgPort,
		)
		return nil
	}

	// (1) interface rule — allow everything WireGuard has already decrypted.
	ifOut, ifErr := exec.Command("ufw", "allow", "in", "on", ifName).CombinedOutput()
	if ifErr != nil {
		return fmt.Errorf("allowMeshInbound: ufw allow in on %s: %w: %s", ifName, ifErr, strings.TrimSpace(string(ifOut)))
	}

	// (2) WG transport port rule (physical NIC) — so a fresh inbound
	// handshake-init from a peer with no prior conntrack state isn't dropped.
	portSpec := fmt.Sprintf("%d/udp", wgPort)
	portOut, portErr := exec.Command("ufw", "allow", "in", portSpec).CombinedOutput()
	if portErr != nil {
		return fmt.Errorf("allowMeshInbound: ufw allow in %s: %w: %s", portSpec, portErr, strings.TrimSpace(string(portOut)))
	}

	slog.Info("tunnel: mesh inbound ufw rules ensured",
		"interface", ifName, "wg_port", wgPort,
	)
	return nil
}

// removeMeshInbound deletes both rules added by allowMeshInbound. Best-effort:
// ufw not installed, the rule already gone, or ufw refusing the delete are all
// swallowed (debug-logged) — matching firewall_windows.go's contract that a
// missing rule on teardown is never an error.
func removeMeshInbound(ifName string, wgPort int) error {
	if _, err := exec.LookPath("ufw"); err != nil {
		return nil
	}
	if out, err := exec.Command("ufw", "delete", "allow", "in", "on", ifName).CombinedOutput(); err != nil {
		slog.Debug("tunnel: removeMeshInbound interface rule (ignored)",
			"interface", ifName, "error", err, "out", strings.TrimSpace(string(out)),
		)
	}
	portSpec := fmt.Sprintf("%d/udp", wgPort)
	if out, err := exec.Command("ufw", "delete", "allow", "in", portSpec).CombinedOutput(); err != nil {
		slog.Debug("tunnel: removeMeshInbound port rule (ignored)",
			"wg_port", wgPort, "error", err, "out", strings.TrimSpace(string(out)),
		)
	}
	return nil
}
