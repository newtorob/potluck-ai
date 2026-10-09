package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

var quietTimings = selfRestartTimings{
	poll:      30 * time.Second,
	settle:    2 * time.Second,
	quiet:     10 * time.Second,
	forceExit: 20 * time.Second,
}

// newTestRestarter is a restarter whose shutdown and exit are recorded instead
// of acted on, on a clock that only moves when the test moves it.
func newTestRestarter(shutdowns *atomic.Int32, requested *atomic.Bool, exited chan int) (*restarter, *fakeClock) {
	clk := newFakeClock()
	return &restarter{
		shutdown:  func() { shutdowns.Add(1) },
		exit:      func(code int) { exited <- code },
		clock:     clk,
		forceExit: quietTimings.forceExit,
		requested: requested,
	}, clk
}

// watcherFixture is an exeWatcher over a real temporary file, with the
// shutdown and exit functions recorded instead of acted on. hashCalls counts
// hashes taken after the startup one.
type watcherFixture struct {
	t         *testing.T
	path      string
	w         *exeWatcher
	restart   *restarter
	clock     *fakeClock
	shutdowns atomic.Int32
	exited    chan int
	requested atomic.Bool
	hashCalls atomic.Int32
}

func newFixture(t *testing.T, initial string) *watcherFixture {
	t.Helper()
	f := &watcherFixture{t: t, path: filepath.Join(t.TempDir(), "potluck-mesh"), exited: make(chan int, 4)}
	f.restart, f.clock = newTestRestarter(&f.shutdowns, &f.requested, f.exited)
	writeFile(t, f.path, initial)
	w, err := newExeWatcher(f.path, quietTimings, f.restart)
	if err != nil {
		t.Fatalf("newExeWatcher: %v", err)
	}
	inner := w.hashFn
	w.hashFn = func(p string) (string, error) {
		f.hashCalls.Add(1)
		return inner(p)
	}
	f.w = w
	return f
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatal(err)
	}
}

// replace swaps the file the way an installer does: write a sibling, then
// rename it over the original, so the path gets a new file.
func (f *watcherFixture) replace(content string) {
	f.t.Helper()
	tmp := f.path + ".new"
	writeFile(f.t, tmp, content)
	if err := os.Rename(tmp, f.path); err != nil {
		f.t.Fatal(err)
	}
}

func (f *watcherFixture) assertNotFired(at time.Time) {
	f.t.Helper()
	if f.w.check(at) {
		f.t.Fatalf("watcher fired at %v, want no exit", at)
	}
	if n := f.shutdowns.Load(); n != 0 {
		f.t.Fatalf("shutdown called %d times, want 0", n)
	}
}

var epoch = time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)

// ---------------------------------------------------------------------------
// the watcher's decisions
// ---------------------------------------------------------------------------

func TestExeWatcher_UnchangedFileNeverFires(t *testing.T) {
	f := newFixture(t, "version one")
	for i := 0; i < 50; i++ {
		f.assertNotFired(epoch.Add(time.Duration(i) * 30 * time.Second))
	}
	if n := f.hashCalls.Load(); n != 0 {
		t.Fatalf("hashed %d times after startup, want none", n)
	}
}

func TestExeWatcher_ReplacedWithDifferentContentExitsOnceAfterQuietPeriod(t *testing.T) {
	f := newFixture(t, "version one")
	f.assertNotFired(epoch)

	f.replace("version two, a different binary")

	// First poll that sees the change starts the quiet period.
	f.assertNotFired(epoch.Add(30 * time.Second))
	seen := epoch.Add(30 * time.Second)
	f.assertNotFired(seen.Add(2 * time.Second))
	f.assertNotFired(seen.Add(9 * time.Second))
	if n := f.hashCalls.Load(); n != 0 {
		t.Fatalf("hashed during the quiet period (%d hashes)", n)
	}

	if !f.w.check(seen.Add(10 * time.Second)) {
		t.Fatal("watcher did not fire once the file had been unchanged for the quiet period")
	}
	if n := f.shutdowns.Load(); n != 1 {
		t.Fatalf("shutdown called %d times, want 1", n)
	}
	if !f.requested.Load() {
		t.Fatal("requested flag not set, main would exit 0")
	}

	// Later polls keep reporting fired but never shut down twice.
	for i := 1; i <= 5; i++ {
		if !f.w.check(seen.Add(10*time.Second + time.Duration(i)*30*time.Second)) {
			t.Fatal("watcher stopped reporting fired")
		}
	}
	if n := f.shutdowns.Load(); n != 1 {
		t.Fatalf("shutdown called %d times after later polls, want 1", n)
	}
	select {
	case code := <-f.exited:
		t.Fatalf("check must not exit the process itself, got exit(%d)", code)
	default:
	}
}

func TestExeWatcher_SameContentRewrittenDoesNotFire(t *testing.T) {
	cases := map[string]func(f *watcherFixture){
		"new file, same bytes": func(f *watcherFixture) { f.replace("version one") },
		"in place, new mtime": func(f *watcherFixture) {
			writeFile(f.t, f.path, "version one")
			later := time.Now().Add(2 * time.Hour)
			if err := os.Chtimes(f.path, later, later); err != nil {
				f.t.Fatal(err)
			}
		},
	}
	for name, rewrite := range cases {
		t.Run(name, func(t *testing.T) {
			f := newFixture(t, "version one")
			rewrite(f)
			for i := 1; i <= 20; i++ {
				f.assertNotFired(epoch.Add(time.Duration(i) * 5 * time.Second))
			}
			// It was hashed once after settling, found equal, and adopted as the
			// new baseline, so later polls do not read the file again.
			if n := f.hashCalls.Load(); n != 1 {
				t.Fatalf("hashed %d times after startup, want 1", n)
			}
		})
	}
}

func TestExeWatcher_MissingFileNeverFires(t *testing.T) {
	f := newFixture(t, "version one")
	if err := os.Remove(f.path); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 100; i++ {
		f.assertNotFired(epoch.Add(time.Duration(i) * 30 * time.Second))
	}
}

func TestExeWatcher_FileBackAfterBeingMissingStartsAFreshQuietPeriod(t *testing.T) {
	f := newFixture(t, "version one")
	f.replace("version two")
	f.assertNotFired(epoch.Add(30 * time.Second)) // change seen

	// The bundle is being swapped again: the path disappears for a while.
	if err := os.Remove(f.path); err != nil {
		t.Fatal(err)
	}
	f.assertNotFired(epoch.Add(38 * time.Second))
	f.assertNotFired(epoch.Add(60 * time.Second))

	f.replace("version two")
	back := epoch.Add(90 * time.Second)
	f.assertNotFired(back)
	f.assertNotFired(back.Add(9 * time.Second))
	if !f.w.check(back.Add(10 * time.Second)) {
		t.Fatal("watcher did not fire 10s after the file came back")
	}
}

func TestExeWatcher_FileStillChangingNeverFiresUntilItSettles(t *testing.T) {
	f := newFixture(t, "version one")
	now := epoch
	// An installer writing the file in pieces: it differs on every poll.
	for i := 0; i < 30; i++ {
		f.replace(strings.Repeat("x", i+1))
		now = now.Add(4 * time.Second)
		f.assertNotFired(now)
	}
	if n := f.hashCalls.Load(); n != 0 {
		t.Fatalf("hashed a file that was still changing (%d hashes)", n)
	}
	// It stops changing. The last poll above saw its final state.
	f.assertNotFired(now.Add(4 * time.Second))
	f.assertNotFired(now.Add(9 * time.Second))
	if !f.w.check(now.Add(10 * time.Second)) {
		t.Fatal("watcher did not fire once the file had settled for the quiet period")
	}
}

func TestExeWatcher_RevertingToTheOriginalCancelsTheChange(t *testing.T) {
	f := newFixture(t, "version one")
	f.replace("version two")
	f.assertNotFired(epoch.Add(5 * time.Second)) // change seen
	if f.w.pending == nil {
		t.Fatal("change not recorded")
	}
	// The path now stats exactly like the file the process started from.
	f.w.statFn = func(string) (os.FileInfo, error) { return f.w.baseline, nil }
	f.assertNotFired(epoch.Add(30 * time.Second))
	f.assertNotFired(epoch.Add(120 * time.Second))
	if f.w.pending != nil {
		t.Fatal("change still pending after the file went back")
	}
}

func TestExeWatcher_HashFailureNeverFires(t *testing.T) {
	f := newFixture(t, "version one")
	f.w.hashFn = func(string) (string, error) { return "", errors.New("read error") }
	f.replace("version two")
	for i := 1; i <= 20; i++ {
		f.assertNotFired(epoch.Add(time.Duration(i) * 5 * time.Second))
	}
}

func TestExeWatcher_FileChangingWhileItIsHashedDoesNotFire(t *testing.T) {
	f := newFixture(t, "version one")
	f.replace("version two")
	inner := f.w.hashFn
	f.w.hashFn = func(p string) (string, error) {
		sum, err := inner(p)
		f.replace("version three, written while the hash ran")
		return sum, err
	}
	f.assertNotFired(epoch.Add(5 * time.Second))
	f.assertNotFired(epoch.Add(15 * time.Second)) // quiet period over: hashes, sees the file move, backs off
	if f.requested.Load() {
		t.Fatal("fired on a hash of a file that changed underneath it")
	}
}

// ---------------------------------------------------------------------------
// the run loop, with an injected clock
// ---------------------------------------------------------------------------

type fakeClock struct {
	mu      sync.Mutex
	now     time.Time
	waiters []*fakeWaiter
	armed   chan struct{}
}

type fakeWaiter struct {
	d  time.Duration
	ch chan time.Time
}

func newFakeClock() *fakeClock { return &fakeClock{now: epoch, armed: make(chan struct{}, 64)} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) After(d time.Duration) <-chan time.Time {
	w := &fakeWaiter{d: d, ch: make(chan time.Time, 1)}
	c.mu.Lock()
	c.waiters = append(c.waiters, w)
	c.mu.Unlock()
	c.armed <- struct{}{}
	return w.ch
}

// wait blocks until the loop has asked for a timer and returns it without
// firing it, so the test can change the world while the loop is idle.
func (c *fakeClock) wait(t *testing.T) *fakeWaiter {
	t.Helper()
	select {
	case <-c.armed:
	case <-time.After(5 * time.Second):
		t.Fatal("the watcher did not arm a timer")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	w := c.waiters[0]
	c.waiters = c.waiters[1:]
	return w
}

// release advances the clock by the timer's duration and fires it.
func (c *fakeClock) release(w *fakeWaiter) {
	c.mu.Lock()
	c.now = c.now.Add(w.d)
	now := c.now
	c.mu.Unlock()
	w.ch <- now
}

func TestExeWatcher_RunPollsSlowlyThenFastThenShutsDownThenForcesExit(t *testing.T) {
	f := newFixture(t, "version one")
	clk := f.clock
	f.w.clock = clk

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { f.w.run(ctx); close(done) }()

	// Idle: one poll every 30s.
	for i := 0; i < 3; i++ {
		w := clk.wait(t)
		if w.d != 30*time.Second {
			t.Fatalf("idle wait %v, want 30s", w.d)
		}
		clk.release(w)
	}

	// The file is replaced while the loop sleeps; the next poll notices it.
	w := clk.wait(t)
	if w.d != 30*time.Second {
		t.Fatalf("wait before noticing %v, want 30s", w.d)
	}
	f.replace("version two")
	clk.release(w)

	// Then it waits the change out on the short interval: five polls, the last
	// one 10s after the change was seen.
	for i := 1; i <= 5; i++ {
		w := clk.wait(t)
		if w.d != 2*time.Second {
			t.Fatalf("settle wait %v, want 2s", w.d)
		}
		if n := f.shutdowns.Load(); n != 0 {
			t.Fatalf("shut down before the quiet period was over (poll %d)", i)
		}
		clk.release(w)
	}

	// The fifth settle poll fired. The loop now gives the shutdown 20s to
	// finish before it forces the exit.
	w = clk.wait(t)
	if w.d != 20*time.Second {
		t.Fatalf("force-exit wait %v, want 20s", w.d)
	}
	if n := f.shutdowns.Load(); n != 1 {
		t.Fatalf("shutdown called %d times, want 1", n)
	}
	select {
	case code := <-f.exited:
		t.Fatalf("exit(%d) before the shutdown had its time", code)
	default:
	}
	clk.release(w)
	select {
	case code := <-f.exited:
		if code != exitSelfRestart {
			t.Fatalf("forced exit code %d, want %d", code, exitSelfRestart)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("process was not forced to exit when the shutdown stalled")
	}
	<-done
}

func TestExeWatcher_RunStopsWhenContextIsDone(t *testing.T) {
	f := newFixture(t, "version one")
	clk := f.clock
	f.w.clock = clk

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { f.w.run(ctx); close(done) }()

	clk.wait(t)
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("run did not return after its context was cancelled")
	}
	select {
	case code := <-f.exited:
		t.Fatalf("exit(%d) called after a normal shutdown", code)
	default:
	}
}

// ---------------------------------------------------------------------------
// the shared restarter (the watcher and POST /restart)
// ---------------------------------------------------------------------------

func TestRestarter_FirstRequestShutsDownOnceAndLaterOnesJoinIt(t *testing.T) {
	var shutdowns atomic.Int32
	var requested atomic.Bool
	exited := make(chan int, 2)
	rs, _ := newTestRestarter(&shutdowns, &requested, exited)

	if !rs.request("restart requested over the local API") {
		t.Fatal("first request did not start the shutdown")
	}
	if rs.request("executable replaced on disk") || rs.request("again") {
		t.Fatal("a later request started a second shutdown")
	}
	if n := shutdowns.Load(); n != 1 {
		t.Fatalf("shutdown called %d times, want 1", n)
	}
	if !requested.Load() {
		t.Fatal("requested flag not set, main would exit 0")
	}
}

func TestRestarter_LeavesAfterForceExitIfTheShutdownStalls(t *testing.T) {
	var shutdowns atomic.Int32
	var requested atomic.Bool
	exited := make(chan int, 2)
	rs, clk := newTestRestarter(&shutdowns, &requested, exited)

	rs.request("restart requested over the local API")
	w := clk.wait(t)
	if w.d != quietTimings.forceExit {
		t.Fatalf("force-exit timer %v, want %v", w.d, quietTimings.forceExit)
	}
	select {
	case code := <-exited:
		t.Fatalf("exit(%d) before the shutdown had its time", code)
	default:
	}
	clk.release(w)
	select {
	case code := <-exited:
		if code != exitSelfRestart {
			t.Fatalf("exit code %d, want %d", code, exitSelfRestart)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("process did not leave after the force-exit time")
	}
}

// ---------------------------------------------------------------------------
// wiring: hash reported, environment switch, exit code
// ---------------------------------------------------------------------------

type captureSink struct {
	mu      sync.Mutex
	sum     string
	n       int
	restart func()
}

func (s *captureSink) SetRestartFunc(fn func()) {
	s.mu.Lock()
	s.restart = fn
	s.mu.Unlock()
}

func (s *captureSink) SetExeSHA256(sum string) {
	s.mu.Lock()
	s.sum, s.n = sum, s.n+1
	s.mu.Unlock()
}

func (s *captureSink) get() (string, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sum, s.n
}

func TestStartSelfRestart_ReportsOwnHashAndWatches(t *testing.T) {
	t.Setenv(envNoSelfRestart, "")
	self, err := ownExecutablePath()
	if err != nil {
		t.Fatal(err)
	}
	want, err := hashFile(self)
	if err != nil {
		t.Fatal(err)
	}

	sink := &captureSink{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if !startSelfRestart(ctx, cancel, sink) {
		t.Fatal("watcher not started")
	}
	got, n := sink.get()
	if n != 1 || got != want {
		t.Fatalf("sink got %q (%d calls), want %q once", got, n, want)
	}
	if len(got) != 64 || got != strings.ToLower(got) {
		t.Fatalf("hash %q is not 64 lowercase hex characters", got)
	}
}

func TestStartSelfRestart_EnvironmentVariableTurnsTheWatcherOffButKeepsTheHash(t *testing.T) {
	for _, v := range []string{"1", "true", "TRUE"} {
		t.Run(v, func(t *testing.T) {
			t.Setenv(envNoSelfRestart, v)
			sink := &captureSink{}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if startSelfRestart(ctx, cancel, sink) {
				t.Fatalf("watcher started with %s=%s", envNoSelfRestart, v)
			}
			if sum, n := sink.get(); n != 1 || len(sum) != 64 {
				t.Fatalf("hash not reported with the watcher off: %q (%d calls)", sum, n)
			}
		})
	}
	for _, v := range []string{"", "0", "false", "no-idea"} {
		t.Run("not "+v, func(t *testing.T) {
			t.Setenv(envNoSelfRestart, v)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if !startSelfRestart(ctx, cancel, &captureSink{}) {
				t.Fatalf("watcher off with %s=%q", envNoSelfRestart, v)
			}
		})
	}
}

func TestStartSelfRestart_RegistersTheRestartCommandWhateverTheWatcherDoes(t *testing.T) {
	// POTLUCK_MESH_NO_SELF_RESTART turns off the binary watcher only. The
	// command is not called here: it would shut the test process down.
	for _, v := range []string{"", "1"} {
		t.Setenv(envNoSelfRestart, v)
		sink := &captureSink{}
		ctx, cancel := context.WithCancel(context.Background())
		startSelfRestart(ctx, cancel, sink)
		sink.mu.Lock()
		registered := sink.restart != nil
		sink.mu.Unlock()
		cancel()
		if !registered {
			t.Errorf("restart command not registered with %s=%q", envNoSelfRestart, v)
		}
	}
}

func TestExitCodeAfterRun(t *testing.T) {
	t.Cleanup(func() { selfRestartRequested.Store(false) })

	selfRestartRequested.Store(false)
	for _, code := range []int{0, 1, 2} {
		if got := exitCodeAfterRun(code); got != code {
			t.Errorf("without a request, run()=%d became %d", code, got)
		}
	}

	selfRestartRequested.Store(true)
	if got := exitCodeAfterRun(0); got != exitSelfRestart || got == 0 {
		t.Errorf("a requested restart exits %d, want non-zero %d", got, exitSelfRestart)
	}
	if got := exitCodeAfterRun(1); got != 1 {
		t.Errorf("a failure during a requested shutdown became %d, want 1", got)
	}
}

// ---------------------------------------------------------------------------
// a real process, a real replaced file
// ---------------------------------------------------------------------------

// TestSelfRestartHelperProcess is the body of the child process started by
// TestSelfRestart_RealProcess. It does nothing in a normal test run.
func TestSelfRestartHelperProcess(t *testing.T) {
	if os.Getenv("POTLUCK_SELFRESTART_HELPER") != "1" {
		t.Skip("helper process for TestSelfRestart_RealProcess")
	}
	selfRestartTimingsInUse = selfRestartTimings{
		poll:      40 * time.Millisecond,
		settle:    20 * time.Millisecond,
		quiet:     200 * time.Millisecond,
		forceExit: 10 * time.Second,
	}
	ctx, stop := context.WithCancel(context.Background())
	sink := &captureSink{}
	if !startSelfRestart(ctx, stop, sink) {
		fmt.Println("watcher not started")
		os.Exit(4)
	}
	sum, _ := sink.get()
	fmt.Printf("ready %s\n", sum)
	if os.Getenv("POTLUCK_SELFRESTART_MODE") == "command" {
		// What POST /restart does once the local API has answered 202.
		go func() {
			time.Sleep(100 * time.Millisecond)
			sink.mu.Lock()
			fn := sink.restart
			sink.mu.Unlock()
			fn()
		}()
	}
	select {
	case <-ctx.Done():
		// The same step main() takes after run() returns.
		os.Exit(exitCodeAfterRun(0))
	case <-time.After(30 * time.Second):
		os.Exit(3)
	}
}

// startHelperChild copies the test binary to a temporary path and runs
// TestSelfRestartHelperProcess in the copy. It returns the copy's path, a
// channel that receives the process's exit, and the hash the child reported.
func startHelperChild(t *testing.T, mode string) (child string, done chan error, reportedSum string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("a running executable cannot be replaced by rename on Windows")
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	child = filepath.Join(t.TempDir(), "potluck-mesh-under-test")
	copyFile(t, self, child, nil)

	cmd := exec.Command(child, "-test.run=^TestSelfRestartHelperProcess$")
	cmd.Env = append(envWithout(os.Environ(), envNoSelfRestart), "POTLUCK_SELFRESTART_HELPER=1", "POTLUCK_SELFRESTART_MODE="+mode)
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done = make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	t.Cleanup(func() { _ = cmd.Process.Kill() })

	lines := bufio.NewReader(out)
	line, err := lines.ReadString('\n')
	if err != nil {
		t.Fatalf("child printed no ready line: %v", err)
	}
	go func() { _, _ = io.Copy(io.Discard, lines) }()
	return child, done, strings.TrimSpace(strings.TrimPrefix(line, "ready "))
}

func exitCodeOf(t *testing.T, err error) int {
	t.Helper()
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("child exited without an exit code: %v", err)
	}
	return exitErr.ExitCode()
}

func TestSelfRestart_RealProcess(t *testing.T) {
	child, done, reported := startHelperChild(t, "watch")
	wantSum, err := hashFile(child)
	if err != nil {
		t.Fatal(err)
	}
	if reported != wantSum {
		t.Fatalf("child reported hash %q, want %q", reported, wantSum)
	}

	// Same bytes under a new file: the child stays up through several polls.
	copyFile(t, child, child, nil)
	select {
	case err := <-done:
		t.Fatalf("child exited after its binary was rewritten with the same content: %v", err)
	case <-time.After(1500 * time.Millisecond):
	}

	// Different bytes: the child shuts down and exits with the restart code.
	copyFile(t, child, child, []byte("\nnew release"))
	select {
	case err := <-done:
		if code := exitCodeOf(t, err); code != exitSelfRestart {
			t.Fatalf("child exit code %d, want %d", code, exitSelfRestart)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("child did not exit after its binary was replaced")
	}
}

// The restart command POST /restart runs ends the process the same way, with
// its binary untouched.
func TestSelfRestart_RealProcessRestartCommand(t *testing.T) {
	_, done, _ := startHelperChild(t, "command")
	select {
	case err := <-done:
		if code := exitCodeOf(t, err); code != exitSelfRestart {
			t.Fatalf("child exit code %d, want %d", code, exitSelfRestart)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("child did not exit after the restart command ran")
	}
}

// copyFile writes src's bytes plus extra to a sibling of dst and renames it
// over dst, the way an installer replaces a binary.
func copyFile(t *testing.T, src, dst string, extra []byte) {
	t.Helper()
	b, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	b = append(b, extra...)
	tmp := dst + ".tmp"
	if err := os.WriteFile(tmp, b, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, dst); err != nil {
		t.Fatal(err)
	}
}

func envWithout(env []string, key string) []string {
	var out []string
	for _, kv := range env {
		if !strings.HasPrefix(kv, key+"=") {
			out = append(out, kv)
		}
	}
	return out
}
