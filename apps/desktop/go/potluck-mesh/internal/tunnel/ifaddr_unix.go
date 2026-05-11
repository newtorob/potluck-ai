//go:build linux || darwin

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
	"runtime"
	"strings"
)

// assignInterfaceAddress assigns a /32 host address to the named TUN interface.
//
// Linux: `ip addr add <ip>/32 dev <iface>`
// macOS: `ifconfig <iface> inet <ip> <ip>` (point-to-point; dst = src)
//
// Shelling out is acceptable for v0. Netlink (Linux) and BSD socket ioctls
// (macOS) are cleaner but more code; deferred to post-launch polish.
// TODO(post-launch): replace with native netlink on Linux, syscall.IoctlX on macOS.
func assignInterfaceAddress(ifName, ip string) error {
	var cmd *exec.Cmd

	switch runtime.GOOS {
	case "linux":
		// `ip addr add` is idempotent if the address already exists on modern
		// iproute2 (it returns exit code 2 with "RTNETLINK answers: File exists").
		// We treat that as success.
		cmd = exec.Command("ip", "addr", "add", ip+"/32", "dev", ifName)

	case "darwin":
		// ifconfig point-to-point form: <iface> inet <local> <remote>
		// Setting src == dst makes this a host route.
		cmd = exec.Command("ifconfig", ifName, "inet", ip, ip)

	default:
		return fmt.Errorf("assignInterfaceAddress: unsupported platform %q", runtime.GOOS)
	}

	out, err := cmd.CombinedOutput()
	if err != nil {
		// On Linux, "file exists" (exit code 2) means the address is already
		// assigned — treat it as success.
		if runtime.GOOS == "linux" && strings.Contains(string(out), "File exists") {
			slog.Debug("tunnel: interface address already assigned", "interface", ifName, "ip", ip)
			return nil
		}
		return fmt.Errorf("assign address %s to %s: %w: %s", ip, ifName, err, string(out))
	}

	return nil
}
