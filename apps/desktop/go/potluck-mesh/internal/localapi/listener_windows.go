//go:build windows

package localapi

import (
	"fmt"
	"log/slog"
	"net"
	"os/user"

	"github.com/Microsoft/go-winio"
	"golang.org/x/sys/windows"
)

// pipeName is the Windows named pipe path. The server creates this; Electron
// connects to it. Because this is a server-created pipe, we only need one
// instance active (no pre-created pipeline instances needed here).
const pipeName = `\\.\pipe\potluck-mesh`

// platformListen creates a Windows named pipe listener whose DACL grants
// access to the account the daemon itself runs as, PLUS (when resolvable)
// the interactively logged-in desktop user. The socketPath parameter is
// ignored on Windows — the named pipe path is always pipeName.
//
// This is the Windows analog of listener_unix.go's SUDO_UID/console-user
// socket-chown reconciliation: when this daemon runs as a Windows SERVICE
// (LocalSystem via the app-driven install — see
// packaging/windows/mesh-install-helper.ps1) rather than as the
// interactively logged-in user, user.Current() resolves to the SERVICE
// ACCOUNT's SID (S-1-5-18 for LocalSystem), not the desktop user's. A DACL
// granting access only to that SID would leave Electron (running as the
// logged-in user) unable to connect to the pipe at all — the daemon would
// run, /status would work locally, but the desktop app would show "mesh
// unavailable" forever.
//
// buildPipeSDDL is factored out (pure, no Win32 calls) so the DACL string
// construction is unit-testable without a real console session — see
// listener_windows_test.go.
func platformListen(_ string) (net.Listener, error) {
	u, err := user.Current()
	if err != nil {
		return nil, fmt.Errorf("localapi: get current user for pipe DACL: %w", err)
	}

	// u.Uid is the SID string on Windows (e.g. "S-1-5-18" for LocalSystem,
	// "S-1-5-21-..." for a real user account).
	consoleSID, ok := consoleUserSID()
	if !ok {
		slog.Warn("localapi: could not resolve the active console user's SID — pipe DACL grants access only to the account this daemon runs as; if that account differs from the interactively logged-in desktop user (e.g. this daemon running as a Windows Service), the desktop app will not be able to connect",
			"service_sid", u.Uid)
	}

	sddl := buildPipeSDDL(u.Uid, consoleSID)

	cfg := &winio.PipeConfig{
		SecurityDescriptor: sddl,
		// MessageMode: false — byte stream (compatible with HTTP over the pipe).
	}

	ln, err := winio.ListenPipe(pipeName, cfg)
	if err != nil {
		return nil, fmt.Errorf("localapi: listen named pipe %s: %w", pipeName, err)
	}
	if ok && consoleSID != u.Uid {
		slog.Info("localapi: pipe DACL grants access to both the running account and the active console user",
			"service_sid", u.Uid, "console_sid", consoleSID)
	}
	return ln, nil
}

// buildPipeSDDL builds the pipe's security descriptor string:
//
//	D:P   — DACL, protected (no inheritance)
//	A     — Allow ACE
//	GA    — Generic All rights
//
// Always grants ownerSID (the account the daemon itself runs as). Also
// grants consoleSID when it is non-empty and different from ownerSID — the
// common case being ownerSID = LocalSystem (running as a Windows Service)
// and consoleSID = the interactively logged-in desktop user Electron runs
// as. When they're equal (dev: running interactively as the same user that
// will connect) or consoleSID is empty (no resolvable console session), a
// single-ACE DACL is emitted, matching the pre-fix behavior.
func buildPipeSDDL(ownerSID, consoleSID string) string {
	if consoleSID == "" || consoleSID == ownerSID {
		return fmt.Sprintf("D:P(A;;GA;;;%s)", ownerSID)
	}
	return fmt.Sprintf("D:P(A;;GA;;;%s)(A;;GA;;;%s)", ownerSID, consoleSID)
}

// consoleUserSID resolves the SID (string form, e.g. "S-1-5-21-...") of the
// user logged into the active interactive console session, via
// WTSGetActiveConsoleSessionId -> WTSQueryUserToken -> Token.GetTokenUser().
// This is the Windows counterpart to listener_unix.go's
// consoleUsernameLinux/consoleUsernameDarwin.
//
// Returns ok=false when there is no active console session (headless, or no
// one has logged in yet — e.g. right after boot before the Windows Service
// has anyone to hand the pipe off to) or any step fails. That is a normal,
// expected outcome, not a fatal error — mirrors the non-fatal contract in
// listener_unix.go's platformListen.
func consoleUserSID() (sid string, ok bool) {
	sessionID := windows.WTSGetActiveConsoleSessionId()
	// 0xFFFFFFFF means "no active console session" (per WTSGetActiveConsoleSessionId docs).
	if sessionID == 0xFFFFFFFF {
		return "", false
	}

	var token windows.Token
	if err := windows.WTSQueryUserToken(sessionID, &token); err != nil {
		slog.Warn("localapi: WTSQueryUserToken failed for active console session", "session_id", sessionID, "err", err)
		return "", false
	}
	defer token.Close()

	tokenUser, err := token.GetTokenUser()
	if err != nil {
		slog.Warn("localapi: could not read token user for active console session", "session_id", sessionID, "err", err)
		return "", false
	}
	if tokenUser.User.Sid == nil {
		return "", false
	}

	s := tokenUser.User.Sid.String()
	if s == "" {
		return "", false
	}
	return s, true
}

// defaultSocketPath returns the named pipe identifier on Windows. This value
// is what Electron (Node.js) passes to net.createConnection({ path }) — Node
// accepts the \\.\pipe\... syntax directly.
func defaultSocketPath() string {
	return pipeName
}
