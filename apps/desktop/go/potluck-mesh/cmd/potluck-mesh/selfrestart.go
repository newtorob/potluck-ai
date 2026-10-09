package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// Self-restart after the binary is replaced.
//
// The sidecar runs as a system service that outlives the desktop app: a root
// LaunchDaemon on macOS, a systemd unit on Linux, a SYSTEM scheduled task on
// Windows. When the app updates, the file on disk is replaced but the running
// process is still the old one, and nothing restarts it. So a release that
// fixes something in the sidecar would only reach a machine after a reboot.
//
// At startup the sidecar hashes its own executable and reports the hash in
// GET /status (exe_sha256). A watcher then checks the executable's path about
// every 30 seconds. When the file's size, modification time or identity
// changed, it waits until the file has been unchanged for 10 seconds (an
// installer may still be writing it, and on macOS the whole app bundle is
// swapped, so the path can be missing for a moment), hashes it, and if the
// hash differs from the startup hash it shuts the sidecar down the way SIGTERM
// does and exits non-zero. The service definitions restart a process that
// exits non-zero:
//
//   - macOS:   KeepAlive true in ai.trypotluck.mesh.plist (any exit).
//   - Linux:   Restart=on-failure in the unit mesh-install-helper.sh writes.
//   - Windows: the scheduled task's restart settings in mesh-install-helper.ps1
//     (not relied on, see below).
//
// On Windows the task runs a copy under C:\ProgramData that only the elevated
// install helper replaces (after stopping the process), so the watcher does
// not fire there; the desktop app offers the restart instead.
//
// A missing file, a file with the same content, or a file that keeps changing
// never causes an exit. POTLUCK_MESH_NO_SELF_RESTART=1 turns the watcher off
// (development, the Docker harness); the hash is still reported.

const (
	// envNoSelfRestart turns the watcher off when it parses as true.
	envNoSelfRestart = "POTLUCK_MESH_NO_SELF_RESTART"

	// exitSelfRestart is the exit code of a shutdown the watcher asked for. Any
	// non-zero code restarts the service; this one is sysexits' EX_TEMPFAIL
	// ("try again"), which no other path in this binary returns.
	exitSelfRestart = 75
)

// selfRestartTimings are the watcher's intervals. A package variable so the
// process-level test can run the real wiring with short ones.
type selfRestartTimings struct {
	poll      time.Duration // between checks while the file looks unchanged
	settle    time.Duration // between checks while a change is being waited out
	quiet     time.Duration // how long the file must stay unchanged before it is hashed
	forceExit time.Duration // how long a shutdown may take before the process leaves anyway
}

var selfRestartTimingsInUse = selfRestartTimings{
	poll:      30 * time.Second,
	settle:    2 * time.Second,
	quiet:     10 * time.Second,
	forceExit: 20 * time.Second,
}

// selfRestartRequested is set once the watcher has asked for a shutdown. main
// reads it through exitCodeAfterRun to turn run()'s clean return into a
// non-zero exit.
var selfRestartRequested atomic.Bool

// exitCodeAfterRun maps run()'s return value to the process exit code. A clean
// shutdown that the watcher asked for exits non-zero so the service manager
// starts the replacement; every other result passes through unchanged.
func exitCodeAfterRun(code int) int {
	if code == 0 && selfRestartRequested.Load() {
		return exitSelfRestart
	}
	return code
}

// exeHashSink receives the startup hash. *localapi.State implements it.
type exeHashSink interface {
	SetExeSHA256(sum string)
}

// startSelfRestart hashes this process's executable, hands the hash to sink,
// and starts the watcher unless POTLUCK_MESH_NO_SELF_RESTART turns it off.
// shutdown must do what SIGTERM does (cancel the root context). If the
// executable cannot be hashed the hash stays empty and no watcher runs. It
// reports whether the watcher is running.
func startSelfRestart(ctx context.Context, shutdown func(), sink exeHashSink) bool {
	path, err := ownExecutablePath()
	if err != nil {
		slog.Warn("self-restart: cannot locate own executable; exe_sha256 stays empty and the watcher is off", "err", err)
		return false
	}
	w, err := newExeWatcher(path, selfRestartTimingsInUse, shutdown, os.Exit, &selfRestartRequested)
	if err != nil {
		slog.Warn("self-restart: cannot hash own executable; exe_sha256 stays empty and the watcher is off", "path", path, "err", err)
		return false
	}
	sink.SetExeSHA256(w.startHash)
	if selfRestartDisabled() {
		slog.Info("self-restart: watcher off by "+envNoSelfRestart, "path", path)
		return false
	}
	slog.Info("self-restart: watching own executable", "path", path, "sha256", w.startHash)
	go w.run(ctx)
	return true
}

func selfRestartDisabled() bool {
	v, err := strconv.ParseBool(strings.TrimSpace(os.Getenv(envNoSelfRestart)))
	return err == nil && v
}

// ownExecutablePath is the path this process was started from, symlinks resolved.
func ownExecutablePath() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(exe)
}

// hashFile returns the lowercase hex SHA-256 of the file at path.
func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// clock is the watcher's view of time, injectable for tests.
type clock interface {
	Now() time.Time
	After(d time.Duration) <-chan time.Time
}

type systemClock struct{}

func (systemClock) Now() time.Time                         { return time.Now() }
func (systemClock) After(d time.Duration) <-chan time.Time { return time.After(d) }

// exeWatcher decides whether the executable at path was replaced by a
// different one. check is one poll and holds all the logic; run only schedules
// it.
type exeWatcher struct {
	path      string
	startHash string      // SHA-256 of the file when the process started
	baseline  os.FileInfo // the file's stat taken just before startHash

	timings   selfRestartTimings
	clock     clock
	statFn    func(string) (os.FileInfo, error)
	hashFn    func(string) (string, error)
	shutdown  func()
	exit      func(code int)
	requested *atomic.Bool

	// A change that differs from baseline and is being waited out. check and run
	// are the only users and run from one goroutine.
	pending      os.FileInfo
	pendingSince time.Time
	fired        bool
}

// newExeWatcher stats and hashes the file at path. The stat comes first, so a
// file replaced between the two is seen as a change on the first poll.
func newExeWatcher(path string, t selfRestartTimings, shutdown func(), exit func(int), requested *atomic.Bool) (*exeWatcher, error) {
	w := &exeWatcher{
		path:      path,
		timings:   t,
		clock:     systemClock{},
		statFn:    os.Stat,
		hashFn:    hashFile,
		shutdown:  shutdown,
		exit:      exit,
		requested: requested,
	}
	fi, err := w.statFn(path)
	if err != nil {
		return nil, err
	}
	sum, err := w.hashFn(path)
	if err != nil {
		return nil, err
	}
	w.baseline, w.startHash = fi, sum
	return w, nil
}

// sameFile reports whether two stats describe the same file with the same
// size and modification time.
func sameFile(a, b os.FileInfo) bool {
	return a.Size() == b.Size() && a.ModTime().Equal(b.ModTime()) && os.SameFile(a, b)
}

// check runs one poll at time now and reports whether the watcher fired, that
// is whether it asked the process to shut down.
func (w *exeWatcher) check(now time.Time) bool {
	if w.fired {
		return true
	}
	fi, err := w.statFn(w.path)
	if err != nil {
		// Missing (an installer or a bundle swap is between steps) or unreadable.
		// Never an exit. Forget a half-observed change so the quiet period starts
		// over when the file is back.
		w.pending = nil
		return false
	}
	if sameFile(fi, w.baseline) {
		w.pending = nil
		return false
	}
	if w.pending == nil || !sameFile(fi, w.pending) {
		// A new change, or the file changed again: start the quiet period over.
		w.pending, w.pendingSince = fi, now
		return false
	}
	if now.Sub(w.pendingSince) < w.timings.quiet {
		return false
	}

	sum, err := w.hashFn(w.path)
	if err != nil {
		w.pending = nil
		return false
	}
	if again, err := w.statFn(w.path); err != nil || !sameFile(again, fi) {
		// It changed while it was being read; the hash may be of a torn file.
		w.pending = nil
		return false
	}
	if sum == w.startHash {
		// The same bytes under a new timestamp or inode (a re-copy, a touch).
		// Adopt the new stat so it is not hashed again on every poll.
		w.baseline, w.pending = fi, nil
		return false
	}

	w.fired = true
	slog.Warn("self-restart: executable replaced on disk, shutting down so the service manager starts the new one",
		"path", w.path, "running_sha256", w.startHash, "installed_sha256", sum)
	w.requested.Store(true)
	w.shutdown()
	return true
}

// run polls until ctx is done or the watcher fires. After it fires it gives the
// shutdown forceExit to finish and then exits the process, because a service
// that never exits would never pick up the new binary.
func (w *exeWatcher) run(ctx context.Context) {
	for {
		wait := w.timings.poll
		if w.pending != nil {
			wait = w.timings.settle
		}
		select {
		case <-ctx.Done():
			return
		case <-w.clock.After(wait):
		}
		if w.check(w.clock.Now()) {
			<-w.clock.After(w.timings.forceExit)
			slog.Error("self-restart: shutdown did not finish in time, exiting", "after", w.timings.forceExit)
			w.exit(exitSelfRestart)
			return
		}
	}
}
