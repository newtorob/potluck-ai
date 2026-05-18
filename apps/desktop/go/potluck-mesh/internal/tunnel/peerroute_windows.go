//go:build windows

package tunnel

import "log/slog"

// addPeerHostRoute is a no-op stub on Windows. Wintun handles routing of the
// configured allowedips internally — the kernel routing table doesn't need
// per-peer entries the way it does on Linux/macOS where the TUN interface is
// userspace-managed and the kernel only routes the local /32.
//
// TODO: validate this assumption against a real Windows multi-peer setup.
// If Windows ends up needing explicit routes, shell out to `route ADD`.
func addPeerHostRoute(ifName, peerIP string) error {
	slog.Debug("tunnel: peer host route (windows no-op)",
		"interface", ifName,
		"peer_ip", peerIP,
	)
	return nil
}

func removePeerHostRoute(ifName, peerIP string) error {
	slog.Debug("tunnel: peer host route remove (windows no-op)",
		"interface", ifName,
		"peer_ip", peerIP,
	)
	return nil
}
