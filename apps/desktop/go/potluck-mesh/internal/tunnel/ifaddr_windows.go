//go:build windows

package tunnel

import (
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
)

// assignInterfaceAddress assigns a host address to the named Wintun adapter
// via `netsh interface ip add address`.
//
// The /32 equivalent on Windows is expressed as the address + subnet mask
// 255.255.255.255 (host-only route).
//
// netsh is present on all supported Windows versions (Vista+) and does not
// require elevation after the Wintun driver has been installed by the Potluck
// installer. We use `netsh` rather than cgo / iphlpapi so that cross-compilation
// from macOS/Linux with CGO_ENABLED=0 works cleanly in CI.
//
// Idempotency: if the address is already assigned, netsh exits with a non-zero
// code and prints a message containing "already exists" or "The object already
// exists". We detect that case and treat it as success so repeated calls
// (e.g. on reconnect) are safe.
func assignInterfaceAddress(ifName, ip string) error {
	// netsh interface ip add address "<ifName>" <ip> 255.255.255.255
	//
	// Using the interface name rather than index is more portable - wg-go
	// names the Wintun adapter using whatever string was passed to
	// tun.CreateTUN, which is the value of tunInterfaceName() ("Wintun").
	// The caller (Tunnel.SetMeshIP) always passes t.ifName, which is the
	// actual name resolved from tdev.Name() after device creation.
	cmd := exec.Command(
		"netsh", "interface", "ip", "add", "address",
		ifName, ip, "255.255.255.255",
	)

	out, err := cmd.CombinedOutput()
	if err != nil {
		outStr := strings.ToLower(string(out))
		// Detect "already assigned" conditions. netsh error messages vary
		// slightly across Windows versions; check for common substrings.
		if strings.Contains(outStr, "already exists") ||
			strings.Contains(outStr, "object already exists") ||
			strings.Contains(outStr, "duplicate") {
			slog.Debug("tunnel: Windows interface address already assigned (idempotent)",
				"interface", ifName,
				"ip", ip,
			)
			return nil
		}
		return fmt.Errorf("netsh add address %s to %q: %w: %s", ip, ifName, err, string(out))
	}

	return nil
}
