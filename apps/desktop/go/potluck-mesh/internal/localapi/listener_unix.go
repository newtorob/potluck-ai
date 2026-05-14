//go:build !windows

package localapi

import (
	"fmt"
	"net"
	"os"
	"runtime"
	"strconv"
)

// platformListen creates a Unix domain socket listener at the given path.
// If the socket file already exists (from a prior sidecar run), it is removed
// before listening so we get a clean bind.
//
// After Listen succeeds the socket file is chmod'd to 0600 (owner-read/write
// only) so that only the running user can connect. This is the primary
// same-user enforcement mechanism on macOS and Linux.
func platformListen(socketPath string) (net.Listener, error) {
	// Remove stale socket file from a previous run.
	if err := os.Remove(socketPath); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("localapi: remove stale socket %s: %w", socketPath, err)
	}

	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("localapi: listen on unix socket %s: %w", socketPath, err)
	}

	// Restrict to owner-only access.
	if err := os.Chmod(socketPath, 0600); err != nil {
		ln.Close()
		return nil, fmt.Errorf("localapi: chmod socket %s: %w", socketPath, err)
	}

	// Dev-mode workaround for the daemon-user vs Electron-user reconciliation
	// (IPC doc open question #1). On macOS the sidecar needs root in dev to
	// create utun, but Electron runs as the logged-in user. When started via
	// sudo, $SUDO_UID points at that user — chown the socket so Electron can
	// open it without us having to slacken mode to 0666.
	//
	// Production daemon-install scripts (launchd / systemd) handle this
	// differently — dedicated daemon user, socket-group membership — so this
	// branch only fires when SUDO_UID is present.
	if sudoUID := os.Getenv("SUDO_UID"); sudoUID != "" {
		if uid, parseErr := strconv.Atoi(sudoUID); parseErr == nil {
			gid := uid
			if sudoGID := os.Getenv("SUDO_GID"); sudoGID != "" {
				if g, parseErr := strconv.Atoi(sudoGID); parseErr == nil {
					gid = g
				}
			}
			if err := os.Chown(socketPath, uid, gid); err != nil {
				ln.Close()
				return nil, fmt.Errorf("localapi: chown socket to SUDO_UID %s: %w", sudoUID, err)
			}
		}
	}

	return ln, nil
}

// defaultSocketPath returns the platform-appropriate Unix socket path.
//   - macOS: /tmp/potluck-mesh.sock  (XDG_RUNTIME_DIR is unreliable under launchd)
//   - Linux: ${XDG_RUNTIME_DIR:-/run/user/$UID}/potluck-mesh.sock
func defaultSocketPath() string {
	switch runtime.GOOS {
	case "darwin":
		return "/tmp/potluck-mesh.sock"
	default: // linux
		dir := os.Getenv("XDG_RUNTIME_DIR")
		if dir == "" {
			uid := os.Getuid()
			dir = "/run/user/" + strconv.Itoa(uid)
		}
		return dir + "/potluck-mesh.sock"
	}
}
