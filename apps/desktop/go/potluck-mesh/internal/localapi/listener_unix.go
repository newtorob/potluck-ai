//go:build !windows

package localapi

import (
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"os/user"
	"runtime"
	"strconv"
	"strings"
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

	// Reconcile the daemon-user vs Electron-user split so a root IPC socket
	// is still reachable by the logged-in desktop user.
	//
	// This only fires when we're ROOT: a non-root process can't chown to
	// another uid (EPERM), and when the mesh runs as a dedicated service user
	// (systemd: User=potluck + CAP_NET_ADMIN) the socket is already owned by
	// the running user, so the chown is both impossible and unnecessary.
	// Crucially, a chown failure must NEVER be fatal: the socket already
	// works (chmod 0600, owned by us). Treating it as fatal here is what
	// crash-looped the IPC server on headless service nodes.
	//
	// Two owner sources, tried in order via decideSocketOwner:
	//  1. SUDO_UID/SUDO_GID — set only when launched via the `sudo` command
	//     (dev-mode workaround: the sidecar needs root in dev to create
	//     utun, but Electron runs as the logged-in user).
	//  2. The active console/login-session user — the path that matters for
	//     a ROOT LaunchDaemon (macOS) / systemd service (Linux) started by
	//     launchd/systemd directly, NOT via `sudo`, where SUDO_UID is never
	//     set. Resolved via consoleUserResolver (platform-specific; see
	//     consoleUsername below). Overridable in tests.
	//
	// If neither source resolves (e.g. a genuinely headless server with no
	// one logged in), the socket is left root-owned and a warning is logged
	// rather than failing — matches the non-fatal contract above.
	if uid, gid, source, ok := decideSocketOwner(os.Geteuid(), os.Getenv("SUDO_UID"), os.Getenv("SUDO_GID"), consoleUserResolver); ok {
		if err := os.Chown(socketPath, uid, gid); err != nil {
			slog.Warn("localapi: could not chown socket to resolved owner (non-fatal); a same-user desktop app may not reach this socket",
				"source", source, "uid", uid, "gid", gid, "socket", socketPath, "err", err)
		} else {
			slog.Info("localapi: chowned IPC socket for cross-user access",
				"source", source, "uid", uid, "gid", gid, "socket", socketPath)
		}
	} else if os.Geteuid() == 0 {
		slog.Warn("localapi: running as root with no SUDO_UID and no resolvable console/login user — IPC socket remains root-owned; a same-user desktop app will not be able to connect (expected on a headless server)",
			"socket", socketPath)
	}

	return ln, nil
}

// decideSocketOwner computes the (uid, gid) the IPC socket should be
// chown'd to, and where that decision came from, given the current
// process's effective uid, the SUDO_UID/SUDO_GID environment (set only
// under a dev `sudo` invocation), and a console-user resolver (used for the
// launchd/systemd ROOT-service case, where SUDO_UID is never set).
//
// Factored out of platformListen so this decision is unit-testable without
// actually running as root or having a real console/login session — see
// listener_unix_test.go, which injects fake euid/env/resolver values.
//
// resolver may be nil (treated as "no console user found").
func decideSocketOwner(euid int, sudoUID, sudoGID string, resolver func() (uid, gid int, ok bool)) (uid, gid int, source string, shouldChown bool) {
	if euid != 0 {
		return 0, 0, "", false
	}

	if sudoUID != "" {
		if parsedUID, err := strconv.Atoi(sudoUID); err == nil {
			g := parsedUID
			if sudoGID != "" {
				if parsedGID, err := strconv.Atoi(sudoGID); err == nil {
					g = parsedGID
				}
			}
			return parsedUID, g, "sudo_uid", true
		}
		// SUDO_UID set but unparseable (shouldn't happen under a real sudo
		// invocation) — fall through to console-user resolution rather than
		// silently leaving the socket root-owned.
	}

	if resolver != nil {
		if u, g, ok := resolver(); ok {
			return u, g, "console_user", true
		}
	}

	return 0, 0, "", false
}

// consoleUserResolver resolves the (uid, gid) of the currently active
// console/login-session user for the launchd/systemd ROOT-service case
// (SUDO_UID unset). A package-level var — not a plain function — so tests
// can inject a fake resolver without needing root, a real console session,
// or platform CLI tools (stat/loginctl/who/logname) to be present.
//
// Returns ok=false when no console user can be determined (e.g. a headless
// server with no one logged in) — a normal, expected outcome, not an error
// worth surfacing beyond platformListen's warning log.
var consoleUserResolver = defaultConsoleUserResolver

// defaultConsoleUserResolver is consoleUserResolver's real implementation:
// resolves the platform-specific console USERNAME (consoleUsername), then
// looks up that username's (uid, gid) via os/user.
func defaultConsoleUserResolver() (uid, gid int, ok bool) {
	username, err := consoleUsername()
	if err != nil || username == "" {
		return 0, 0, false
	}
	u, err := user.Lookup(username)
	if err != nil {
		slog.Warn("localapi: resolved a console username but could not look it up",
			"username", username, "err", err)
		return 0, 0, false
	}
	parsedUID, err1 := strconv.Atoi(u.Uid)
	parsedGID, err2 := strconv.Atoi(u.Gid)
	if err1 != nil || err2 != nil {
		slog.Warn("localapi: console user has a non-numeric uid/gid, cannot chown",
			"username", username, "uid", u.Uid, "gid", u.Gid)
		return 0, 0, false
	}
	return parsedUID, parsedGID, true
}

// consoleUsername resolves the username of the currently active
// console/login-session user via a platform-specific mechanism. Returns an
// error when no such user can be determined — the normal outcome on a
// headless server with no one logged in, NOT a bug.
func consoleUsername() (string, error) {
	switch runtime.GOOS {
	case "darwin":
		return consoleUsernameDarwin()
	case "linux":
		return consoleUsernameLinux()
	default:
		return "", fmt.Errorf("console-user resolution not implemented for GOOS=%s", runtime.GOOS)
	}
}

// consoleUsernameDarwin shells out to `stat -f%Su /dev/console` — the
// standard way to ask "who owns the console device right now" on macOS,
// which is the logged-in user at the physical console (or a
// screen-sharing session). SCDynamicStoreCopyConsoleUser (via cgo) is the
// "proper" API for this but pulls in the SystemConfiguration framework for
// one string; the shell invocation gives the same answer and is simpler.
func consoleUsernameDarwin() (string, error) {
	out, err := exec.Command("stat", "-f%Su", "/dev/console").Output()
	if err != nil {
		return "", fmt.Errorf("stat -f%%Su /dev/console: %w", err)
	}
	username, ok := parseStatConsoleOutput(string(out))
	if !ok {
		return "", fmt.Errorf("no console user logged in (stat reported %q)", strings.TrimSpace(string(out)))
	}
	return username, nil
}

// parseStatConsoleOutput parses the output of `stat -f%Su /dev/console`
// (macOS). A pure function (no exec) so it's directly unit-testable.
// "root" is treated as "nobody logged in" (the loginwindow's owner),
// not a real console user to chown to.
func parseStatConsoleOutput(out string) (username string, ok bool) {
	username = strings.TrimSpace(out)
	if username == "" || username == "root" {
		return "", false
	}
	return username, true
}

// consoleUsernameLinux resolves the active graphical/login-session user via
// `loginctl` (systemd-logind's session list), preferring a seat0 (physical
// console) session. Falls back to `who` (any session with a controlling
// terminal), then `logname`, for non-systemd or otherwise unusual
// environments.
func consoleUsernameLinux() (string, error) {
	if out, err := exec.Command("loginctl", "list-sessions", "--no-legend").Output(); err == nil {
		if username, ok := parseLoginctlSessions(string(out)); ok {
			return username, nil
		}
	}
	if out, err := exec.Command("who").Output(); err == nil {
		if username, ok := parseWhoOutput(string(out)); ok {
			return username, nil
		}
	}
	if out, err := exec.Command("logname").Output(); err == nil {
		if username := strings.TrimSpace(string(out)); username != "" {
			return username, nil
		}
	}
	return "", fmt.Errorf("no active login session found (loginctl/who/logname all failed or returned nothing)")
}

// parseLoginctlSessions parses `loginctl list-sessions --no-legend` output
// (whitespace-separated columns: SESSION UID USER [SEAT]) and returns the
// username of the seat0 (physical console) session if one is listed,
// otherwise the first listed session's username. A pure function (no exec)
// so it's directly unit-testable against canned loginctl output.
func parseLoginctlSessions(out string) (username string, ok bool) {
	var fallback string
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) < 3 {
			continue
		}
		user := fields[2]
		if fallback == "" {
			fallback = user
		}
		if len(fields) >= 4 && fields[3] == "seat0" {
			return user, true
		}
	}
	if fallback != "" {
		return fallback, true
	}
	return "", false
}

// parseWhoOutput parses `who` output (columns: USER TTY DATE TIME ...) and
// returns the first listed user. A pure function, unit-testable.
func parseWhoOutput(out string) (username string, ok bool) {
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 1 && fields[0] != "" {
			return fields[0], true
		}
	}
	return "", false
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
