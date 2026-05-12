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
