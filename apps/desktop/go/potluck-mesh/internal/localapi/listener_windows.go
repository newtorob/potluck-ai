//go:build windows

package localapi

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os/user"
	"sync"
	"time"

	"github.com/Microsoft/go-winio"
	"golang.org/x/sys/windows"
)

// pipeName is the Windows named pipe path. The server creates this; Electron
// connects to it. Because this is a server-created pipe, we only need one
// instance active (no pre-created pipeline instances needed here).
const pipeName = `\\.\pipe\potluck-mesh`

// consolePollInterval is how often the pipe checks who is signed in at the
// console. It is a variable so tests can shorten it.
var consolePollInterval = 3 * time.Second

// platformListen creates a Windows named pipe listener whose DACL grants
// access to the account the daemon itself runs as, PLUS the user signed in
// at the active console, and keeps that second grant current for as long as
// the listener lives. The socketPath parameter is ignored on Windows — the
// named pipe path is always pipeName.
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
// The scheduled task starts at boot before anyone has signed in, so the
// console user at bind time is often nobody. The DACL is therefore rewritten
// in place whenever the console user changes (nobody -> user, user -> nobody,
// user -> user): see listenPipe and listener_accessfollow.go.
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
	return listenPipe(pipeName, u.Uid, consoleKey, consolePollInterval)
}

// listenPipe opens the named pipe once, with a DACL for ownerSID plus the
// current key (a console user's SID, or "" for nobody), and starts an
// accessFollower that rewrites that DACL in place when key() changes. The
// pipe is never closed and re-created to change who may connect, so
// connections already open are unaffected and the pipe name never goes away.
// Closing the returned listener stops the follower.
func listenPipe(name, ownerSID string, key func() string, interval time.Duration) (net.Listener, error) {
	current := key()
	ln, err := winio.ListenPipe(name, &winio.PipeConfig{
		SecurityDescriptor: buildPipeSDDL(ownerSID, current),
		// MessageMode: false — byte stream (compatible with HTTP over the pipe).
	})
	if err != nil {
		return nil, fmt.Errorf("localapi: listen named pipe %s: %w", name, err)
	}
	switch {
	case current == "":
		slog.Info("localapi: no console user, pipe DACL grants access only to the account this daemon runs as; it will follow the next sign-in",
			"service_sid", ownerSID)
	case current != ownerSID:
		slog.Info("localapi: pipe DACL grants access to both the running account and the active console user",
			"service_sid", ownerSID, "console_sid", current)
	}

	apply := func(ctx context.Context, consoleSID string) error {
		start := time.Now()
		if err := setPipeDACL(ctx, name, buildPipeSDDL(ownerSID, consoleSID)); err != nil {
			return err
		}
		if consoleSID == "" {
			slog.Info("localapi: console user gone, pipe DACL updated in place to grant access only to the account this daemon runs as",
				"service_sid", ownerSID, "took_us", time.Since(start).Microseconds())
		} else {
			slog.Info("localapi: console user changed, pipe DACL updated in place to grant the running account and the new console user",
				"service_sid", ownerSID, "console_sid", consoleSID, "took_us", time.Since(start).Microseconds())
		}
		return nil
	}
	return &pipeListener{
		Listener: ln,
		follower: startAccessFollower(current, key, apply, interval),
	}, nil
}

// pipeListener is the winio listener plus the follower that keeps its DACL
// current. Accept and Addr are the winio listener's own.
type pipeListener struct {
	net.Listener
	follower *accessFollower
}

func (p *pipeListener) Close() error {
	p.follower.stop()
	return p.Listener.Close()
}

// setPipeDACL replaces the DACL of the named pipe name with sddl's, without
// touching the listener. It opens the pipe by name with WRITE_DAC, the same
// way a client would, and sets the security descriptor on that handle. The
// descriptor belongs to the pipe, not to the handle, so it applies to every
// later open while connections that are already open keep working.
//
// The self-open appears to the server as one accepted connection that closes
// at once, which http.Server drops without logging. ERROR_PIPE_BUSY (no
// instance is waiting at this instant) is retried for up to two seconds.
func setPipeDACL(ctx context.Context, name, sddl string) error {
	name16, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	var h windows.Handle
	for tries := 0; ; tries++ {
		h, err = windows.CreateFile(name16, windows.WRITE_DAC|windows.READ_CONTROL, 0, nil, windows.OPEN_EXISTING, 0, 0)
		if !errors.Is(err, windows.ERROR_PIPE_BUSY) {
			break
		}
		if tries >= 400 {
			return fmt.Errorf("open pipe %s for WRITE_DAC: still busy after 2s: %w", name, err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Millisecond):
		}
	}
	if err != nil {
		return fmt.Errorf("open pipe %s for WRITE_DAC: %w", name, err)
	}
	defer windows.CloseHandle(h)

	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return fmt.Errorf("parse pipe DACL: %w", err)
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return fmt.Errorf("read pipe DACL: %w", err)
	}
	if err := windows.SetSecurityInfo(h, windows.SE_KERNEL_OBJECT,
		windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, dacl, nil); err != nil {
		return fmt.Errorf("set pipe DACL: %w", err)
	}
	return nil
}

// consoleKey is the accessFollower key: the SID of the user signed in at the
// console, or "" when nobody is.
func consoleKey() string {
	sid, _ := consoleUserSID()
	return sid
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
// Returns ok=false when there is no active console session (headless), the
// session has no user (the logon screen, e.g. right after boot), or any step
// fails. None of those is fatal; listener_unix.go's platformListen has the
// same contract. It is called every consolePollInterval, so an unexpected
// failure is logged once per distinct message instead of once per call.
func consoleUserSID() (sid string, ok bool) {
	sessionID := windows.WTSGetActiveConsoleSessionId()
	// 0xFFFFFFFF means "no active console session" (per WTSGetActiveConsoleSessionId docs).
	if sessionID == 0xFFFFFFFF {
		return "", false
	}

	var token windows.Token
	if err := windows.WTSQueryUserToken(sessionID, &token); err != nil {
		// ERROR_NO_TOKEN: the console session exists but nobody is signed
		// in to it (the logon screen). Normal, and silent.
		if !errors.Is(err, windows.ERROR_NO_TOKEN) {
			consoleWarn.warn("localapi: WTSQueryUserToken failed for active console session", err, sessionID)
		}
		return "", false
	}
	defer token.Close()

	tokenUser, err := token.GetTokenUser()
	if err != nil {
		consoleWarn.warn("localapi: could not read token user for active console session", err, sessionID)
		return "", false
	}
	if tokenUser.User.Sid == nil {
		return "", false
	}

	s := tokenUser.User.Sid.String()
	if s == "" {
		return "", false
	}
	consoleWarn.reset()
	return s, true
}

// consoleWarnings remembers the last console-lookup warning so a failure
// that repeats on every poll is logged once. A successful lookup clears it,
// so the same failure after a recovery is reported again.
type consoleWarnings struct {
	mu   sync.Mutex
	last string
}

func (w *consoleWarnings) warn(msg string, err error, sessionID uint32) {
	text := fmt.Sprintf("%s: %v", msg, err)
	w.mu.Lock()
	repeat := w.last == text
	w.last = text
	w.mu.Unlock()
	if !repeat {
		slog.Warn(msg, "session_id", sessionID, "err", err)
	}
}

func (w *consoleWarnings) reset() {
	w.mu.Lock()
	w.last = ""
	w.mu.Unlock()
}

var consoleWarn consoleWarnings

// defaultSocketPath returns the named pipe identifier on Windows. This value
// is what Electron (Node.js) passes to net.createConnection({ path }) — Node
// accepts the \\.\pipe\... syntax directly.
func defaultSocketPath() string {
	return pipeName
}
