//go:build windows

package localapi

import (
	"fmt"
	"net"
	"os/user"

	"github.com/Microsoft/go-winio"
)

// pipeName is the Windows named pipe path. The server creates this; Electron
// connects to it. Because this is a server-created pipe, we only need one
// instance active (no pre-created pipeline instances needed here).
const pipeName = `\\.\pipe\potluck-mesh`

// platformListen creates a Windows named pipe listener confined to the
// current user's SID via a DACL. The socketPath parameter is ignored on
// Windows — the named pipe path is always pipeName.
//
// The DACL "D:P(A;;GA;;;{SID})" grants Generic All to the current user only:
//   - D:P   — DACL, protected (no inheritance)
//   - A     — Allow ACE
//   - GA    — Generic All rights
//   - {SID} — the current user's SID
//
// This prevents other local users (and SYSTEM) from connecting, matching the
// same-user-only invariant enforced by Unix socket permissions.
//
// KNOWN GAP (tracked, not yet fixed — see the SUDO_UID/console-user fix in
// listener_unix.go for the equivalent macOS/Linux problem): when this
// sidecar runs as a Windows SERVICE (LocalSystem or a dedicated service
// account) rather than as the interactively logged-in user, user.Current()
// above resolves to THAT service account's SID, not the desktop user's. The
// DACL then grants pipe access only to the service account, and Electron
// (running as the logged-in user) gets access denied connecting — the exact
// same class of bug the SUDO_UID gap was for Unix, just via a different
// mechanism (DACL vs. file chmod/chown).
//
// Fixing this properly needs more than a small change: resolve the active
// interactive session's user SID via WTSGetActiveConsoleSessionId +
// WTSQueryUserToken (or WTSEnumerateSessions + per-session
// WTSQuerySessionInformation for WTSUserName), translate that to a SID via
// LookupAccountName, and grant the DACL to BOTH the service account and that
// SID (so the service can still administer the pipe and the desktop user can
// connect to it). That's a real Win32/golang.org/x/sys/windows undertaking —
// deliberately left out of this change; flagging here so it isn't silently
// assumed to work once the sidecar ships as a Windows service.
func platformListen(_ string) (net.Listener, error) {
	u, err := user.Current()
	if err != nil {
		return nil, fmt.Errorf("localapi: get current user for pipe DACL: %w", err)
	}

	// u.Uid is the SID string on Windows (e.g. "S-1-5-21-...").
	sddl := fmt.Sprintf("D:P(A;;GA;;;%s)", u.Uid)

	cfg := &winio.PipeConfig{
		SecurityDescriptor: sddl,
		// MessageMode: false — byte stream (compatible with HTTP over the pipe).
	}

	ln, err := winio.ListenPipe(pipeName, cfg)
	if err != nil {
		return nil, fmt.Errorf("localapi: listen named pipe %s: %w", pipeName, err)
	}
	return ln, nil
}

// defaultSocketPath returns the named pipe identifier on Windows. This value
// is what Electron (Node.js) passes to net.createConnection({ path }) — Node
// accepts the \\.\pipe\... syntax directly.
func defaultSocketPath() string {
	return pipeName
}
