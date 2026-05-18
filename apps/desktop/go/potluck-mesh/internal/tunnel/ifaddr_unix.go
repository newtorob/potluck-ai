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
		} else {
			return fmt.Errorf("assign address %s to %s: %w: %s", ip, ifName, err, string(out))
		}
	}

	// Linux: explicitly bring the interface UP. `ip addr add` does NOT do this
	// (unlike macOS ifconfig, which auto-ups the interface when an address is
	// assigned). A DOWN interface can't accept route installations — `ip route
	// add ... dev <iface>` fails with "Device for nexthop is not up".
	if runtime.GOOS == "linux" {
		upCmd := exec.Command("ip", "link", "set", ifName, "up")
		if upOut, upErr := upCmd.CombinedOutput(); upErr != nil {
			return fmt.Errorf("bring up %s: %w: %s", ifName, upErr, string(upOut))
		}
		slog.Debug("tunnel: interface brought up", "interface", ifName)
	}

	return nil
}
