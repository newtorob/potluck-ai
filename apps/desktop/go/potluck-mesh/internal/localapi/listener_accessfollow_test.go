package localapi

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// keySource is a settable access key.
type keySource struct {
	mu sync.Mutex
	v  string
}

func (k *keySource) get() string {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.v
}

func (k *keySource) set(v string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.v = v
}

// applyRecorder is an apply function that records its calls and can be told
// to fail.
type applyRecorder struct {
	mu    sync.Mutex
	keys  []string
	fails []error // consumed one per call; nil entries and an empty list mean success
}

func (r *applyRecorder) apply(_ context.Context, key string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.keys = append(r.keys, key)
	if len(r.fails) > 0 {
		err := r.fails[0]
		r.fails = r.fails[1:]
		return err
	}
	return nil
}

func (r *applyRecorder) calls() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.keys...)
}

func sameKeys(got []string, want ...string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

func eventually(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatal("condition not met within 3s")
}

// warnCounter counts the Warn records slog receives while a test runs.
type warnCounter struct {
	n atomic.Int32
}

func (w *warnCounter) Enabled(context.Context, slog.Level) bool { return true }
func (w *warnCounter) Handle(_ context.Context, r slog.Record) error {
	if r.Level >= slog.LevelWarn {
		w.n.Add(1)
	}
	return nil
}
func (w *warnCounter) WithAttrs([]slog.Attr) slog.Handler { return w }
func (w *warnCounter) WithGroup(string) slog.Handler      { return w }

func countWarnings(t *testing.T) *warnCounter {
	t.Helper()
	w := &warnCounter{}
	old := slog.Default()
	slog.SetDefault(slog.New(w))
	t.Cleanup(func() { slog.SetDefault(old) })
	return w
}

// newDrivenFollower builds a follower with no clock: the test calls step
// itself, which makes every poll deterministic.
func newDrivenFollower(t *testing.T, applied string, apply func(context.Context, string) error) (*accessFollower, *keySource) {
	t.Helper()
	ks := &keySource{v: applied}
	f := startAccessFollowerTicks(applied, ks.get, apply, nil, nil)
	t.Cleanup(func() {
		f.stop()
		waitStopped(t, f)
	})
	return f, ks
}

func waitStopped(t *testing.T, f *accessFollower) {
	t.Helper()
	select {
	case <-f.stopped:
	case <-time.After(2 * time.Second):
		t.Fatal("poll goroutine still running 2s after stop")
	}
}

// ---------------------------------------------------------------------------
// when apply runs
// ---------------------------------------------------------------------------

func TestAccessFollower_NoApplyWhileKeyIsUnchanged(t *testing.T) {
	rec := &applyRecorder{}
	f, _ := newDrivenFollower(t, "S-1-A", rec.apply)
	for i := 0; i < 10; i++ {
		f.step()
	}
	if got := rec.calls(); len(got) != 0 {
		t.Fatalf("apply calls = %q, want none for the key the pipe was created with", got)
	}
}

func TestAccessFollower_AppliesEachChangeOnce(t *testing.T) {
	rec := &applyRecorder{}
	f, ks := newDrivenFollower(t, "", rec.apply)

	// nobody -> A -> B (fast user switch) -> nobody
	for _, k := range []string{"S-1-A", "S-1-B", ""} {
		ks.set(k)
		f.step()
		f.step()
		f.step()
	}
	if got := rec.calls(); !sameKeys(got, "S-1-A", "S-1-B", "") {
		t.Fatalf("apply calls = %q, want one per change: A, B, nobody", got)
	}
}

func TestAccessFollower_PollsOnItsOwn(t *testing.T) {
	rec := &applyRecorder{}
	ks := &keySource{}
	f := startAccessFollower("", ks.get, rec.apply, 2*time.Millisecond)
	t.Cleanup(f.stop)

	ks.set("S-1-A")
	eventually(t, func() bool { return sameKeys(rec.calls(), "S-1-A") })
	time.Sleep(20 * time.Millisecond)
	if got := rec.calls(); !sameKeys(got, "S-1-A") {
		t.Fatalf("apply calls = %q, want exactly one", got)
	}

	f.stop()
	waitStopped(t, f)
}

func TestAccessFollower_ZeroIntervalStillRunsAndStops(t *testing.T) {
	ks := &keySource{}
	f := startAccessFollower("", ks.get, (&applyRecorder{}).apply, 0)
	f.stop()
	waitStopped(t, f)
}

// ---------------------------------------------------------------------------
// failure handling
// ---------------------------------------------------------------------------

func TestAccessFollower_FailedApplyIsRetriedNextTick(t *testing.T) {
	boom := errors.New("access denied")
	rec := &applyRecorder{fails: []error{boom, boom}}
	f, ks := newDrivenFollower(t, "", rec.apply)

	ks.set("S-1-A")
	f.step() // fails
	f.step() // fails again
	f.step() // succeeds
	f.step() // nothing left to do
	f.step()
	if got := rec.calls(); !sameKeys(got, "S-1-A", "S-1-A", "S-1-A") {
		t.Fatalf("apply calls = %q, want two failed tries and one success, then silence", got)
	}
}

func TestAccessFollower_AppliedKeyOnlyAdvancesOnSuccess(t *testing.T) {
	boom := errors.New("busy")
	rec := &applyRecorder{fails: []error{boom}}
	f, ks := newDrivenFollower(t, "S-1-A", rec.apply)

	// The change to B fails. The pipe still matches A, so when the key goes
	// back to A there is nothing to apply.
	ks.set("S-1-B")
	f.step()
	ks.set("S-1-A")
	f.step()
	f.step()
	if got := rec.calls(); !sameKeys(got, "S-1-B") {
		t.Fatalf("apply calls = %q, want only the failed try for B", got)
	}

	// A failure for B followed by a change to C applies C, not B.
	rec.mu.Lock()
	rec.fails = []error{boom}
	rec.mu.Unlock()
	ks.set("S-1-B")
	f.step() // fails
	ks.set("S-1-C")
	f.step() // succeeds
	f.step()
	if got := rec.calls(); !sameKeys(got, "S-1-B", "S-1-B", "S-1-C") {
		t.Fatalf("apply calls = %q, want B (failed), B (failed), C", got)
	}

	// And after C succeeded, going back to A is a real change again.
	ks.set("S-1-A")
	f.step()
	if got := rec.calls(); !sameKeys(got, "S-1-B", "S-1-B", "S-1-C", "S-1-A") {
		t.Fatalf("apply calls = %q, want A applied after C", got)
	}
}

func TestAccessFollower_WarnsOncePerDistinctError(t *testing.T) {
	warns := countWarnings(t)
	e1, e2 := errors.New("pipe busy"), errors.New("access denied")
	rec := &applyRecorder{fails: []error{e1, e1, e1, e2, e2}}
	f, ks := newDrivenFollower(t, "", rec.apply)

	ks.set("S-1-A")
	for i := 0; i < 3; i++ {
		f.step()
	}
	if n := warns.n.Load(); n != 1 {
		t.Fatalf("warnings after the same error three times = %d, want 1", n)
	}
	f.step() // e2: a different error, logged
	f.step() // e2 again, not logged
	if n := warns.n.Load(); n != 2 {
		t.Fatalf("warnings after a second distinct error = %d, want 2", n)
	}

	f.step() // succeeds
	if n := warns.n.Load(); n != 2 {
		t.Fatalf("warnings after the success = %d, want 2", n)
	}

	// After a success the error that was last warned about is news again.
	rec.mu.Lock()
	rec.fails = []error{e2}
	rec.mu.Unlock()
	ks.set("")
	f.step()
	if n := warns.n.Load(); n != 3 {
		t.Fatalf("warnings for a repeat of the last error after a success = %d, want 3", n)
	}
}

// ---------------------------------------------------------------------------
// stop
// ---------------------------------------------------------------------------

func TestAccessFollower_StopEndsTheGoroutineAndIsIdempotent(t *testing.T) {
	ticks := make(chan time.Time)
	var ticksStopped atomic.Bool
	ks := &keySource{}
	f := startAccessFollowerTicks("", ks.get, (&applyRecorder{}).apply, ticks, func() { ticksStopped.Store(true) })

	f.stop()
	f.stop()
	waitStopped(t, f)
	if !ticksStopped.Load() {
		t.Error("the ticker was not stopped")
	}
}

func TestAccessFollower_StopDuringApplyCancelsItAndEndsPromptly(t *testing.T) {
	warns := countWarnings(t)
	ticks := make(chan time.Time)
	entered := make(chan struct{})
	apply := func(ctx context.Context, key string) error {
		close(entered)
		<-ctx.Done() // a well-behaved apply gives up when told to stop
		return ctx.Err()
	}
	ks := &keySource{v: "S-1-A"}
	f := startAccessFollowerTicks("", ks.get, apply, ticks, nil)

	ticks <- time.Now() // the loop starts applying and blocks inside apply
	<-entered

	start := time.Now()
	f.stop()
	waitStopped(t, f)
	if d := time.Since(start); d > time.Second {
		t.Errorf("stop took %s while apply was running", d)
	}
	if n := warns.n.Load(); n != 0 {
		t.Errorf("an apply cancelled by stop was logged as a warning (%d)", n)
	}
}

func TestAccessFollower_StopDoesNotWaitForAnApplyThatIgnoresTheContext(t *testing.T) {
	ticks := make(chan time.Time)
	entered := make(chan struct{})
	release := make(chan struct{})
	apply := func(ctx context.Context, key string) error {
		close(entered)
		<-release
		return nil
	}
	ks := &keySource{v: "S-1-A"}
	f := startAccessFollowerTicks("", ks.get, apply, ticks, nil)

	ticks <- time.Now()
	<-entered

	returned := make(chan struct{})
	go func() { f.stop(); close(returned) }()
	select {
	case <-returned:
	case <-time.After(time.Second):
		t.Fatal("stop hung while apply was running")
	}

	close(release) // apply finally returns; the goroutine must then exit
	waitStopped(t, f)
}

func TestAccessFollower_NoApplyAfterStop(t *testing.T) {
	rec := &applyRecorder{}
	f, ks := newDrivenFollower(t, "", rec.apply)
	f.stop()
	ks.set("S-1-A")
	f.step()
	if got := rec.calls(); len(got) != 0 {
		t.Fatalf("apply calls after stop = %q, want none", got)
	}
}

// ---------------------------------------------------------------------------
// stress, for -race
// ---------------------------------------------------------------------------

func TestAccessFollower_ConcurrentKeyChangesAndStop(t *testing.T) {
	var applies atomic.Int32
	apply := func(ctx context.Context, key string) error {
		applies.Add(1)
		return nil
	}
	ks := &keySource{}
	f := startAccessFollower("", ks.get, apply, time.Millisecond)

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 200; i++ {
			ks.set([]string{"", "S-1-A", "S-1-B"}[i%3])
			time.Sleep(200 * time.Microsecond)
		}
	}()
	wg.Wait()
	f.stop()
	waitStopped(t, f)
	if applies.Load() == 0 {
		t.Error("no change was applied while the key moved")
	}
}
