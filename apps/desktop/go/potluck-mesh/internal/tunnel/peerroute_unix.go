//go:build linux || darwin

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
	"runtime"
	"strings"
)

// addPeerHostRoute installs a /32 host route for the given peer mesh IP via
// the TUN interface so the kernel actually delivers packets to WG. Without
// this, the only routes for the household subnet are the local node's /32
// (assigned via assignInterfaceAddress) - packets to other peers fall through
// to the default gateway and get dropped.
//
// Idempotent: re-running for the same (ip, iface) is a no-op (existing-route
// errors are downgraded to debug).
//
// Linux: `ip route add <ip>/32 dev <iface>`
// macOS: `route -n add -host <ip> -interface <iface>`
//
// TODO: replace with native netlink (Linux) / route-socket (macOS).
func addPeerHostRoute(ifName, peerIP string) error {
	var cmd *exec.Cmd

	switch runtime.GOOS {
	case "linux":
		cmd = exec.Command("ip", "route", "add", peerIP+"/32", "dev", ifName)
	case "darwin":
		cmd = exec.Command("route", "-n", "add", "-host", peerIP, "-interface", ifName)
	default:
		return fmt.Errorf("addPeerHostRoute: unsupported platform %q", runtime.GOOS)
	}

	out, err := cmd.CombinedOutput()
	if err != nil {
		outStr := strings.ToLower(string(out))
		// Treat "already exists" as success.
		// Linux iproute2: "RTNETLINK answers: File exists"
		// macOS route(8): "route: writing to routing socket: File exists"
		if strings.Contains(outStr, "file exists") {
			slog.Debug("tunnel: peer host route already present",
				"interface", ifName,
				"peer_ip", peerIP,
			)
			return nil
		}
		return fmt.Errorf("add host route %s via %s: %w: %s", peerIP, ifName, err, string(out))
	}

	slog.Info("tunnel: peer host route installed",
		"interface", ifName,
		"peer_ip", peerIP,
	)
	return nil
}

// removePeerHostRoute removes the /32 host route installed by addPeerHostRoute.
// Called when a peer is removed from the household so its mesh IP no longer
// black-holes packets to a dead route. Idempotent: missing-route errors are
// downgraded to debug.
func removePeerHostRoute(ifName, peerIP string) error {
	var cmd *exec.Cmd

	switch runtime.GOOS {
	case "linux":
		cmd = exec.Command("ip", "route", "del", peerIP+"/32", "dev", ifName)
	case "darwin":
		cmd = exec.Command("route", "-n", "delete", "-host", peerIP, "-interface", ifName)
	default:
		return fmt.Errorf("removePeerHostRoute: unsupported platform %q", runtime.GOOS)
	}

	out, err := cmd.CombinedOutput()
	if err != nil {
		outStr := strings.ToLower(string(out))
		// "no such process" (Linux) / "not in table" (macOS) - already gone, success.
		if strings.Contains(outStr, "no such process") || strings.Contains(outStr, "not in table") {
			slog.Debug("tunnel: peer host route already absent",
				"interface", ifName,
				"peer_ip", peerIP,
			)
			return nil
		}
		return fmt.Errorf("remove host route %s via %s: %w: %s", peerIP, ifName, err, string(out))
	}
	return nil
}
