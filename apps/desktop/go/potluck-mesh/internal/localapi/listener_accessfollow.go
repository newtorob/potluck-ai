package localapi

import (
	"context"
	"log/slog"
	"time"
)

// defaultFollowInterval is how often an accessFollower asks for the current
// access key when the caller passes no interval.
const defaultFollowInterval = 3 * time.Second

// accessFollower keeps a listener's access rules in step with an "access key".
// On Windows the key is the SID of the user signed in at the console ("" when
// nobody is), and the rules are the named pipe's DACL. The daemon is a SYSTEM
// scheduled task that starts at boot, before anyone has signed in, so the
// user who signs in later has to be let in without a restart.
//
// A goroutine calls key every interval. When the answer differs from the key
// that was last applied it calls apply with the new key. apply changes the
// access rules in place and never touches the listener, so connections that
// are already open are not affected and there is no gap in which the pipe does
// not exist. The file has no Windows code so the follow logic is unit-tested
// on every OS.
//
// If apply fails, the access rules stay as they were, the failure is logged
// once per distinct error, and the same key is tried again on the next tick.
// The applied key only moves forward when apply succeeds.
type accessFollower struct {
	key   func() string
	apply func(ctx context.Context, key string) error

	applied string // the key the access rules currently match; step only
	lastErr string // text of the last warned error; step only

	ctx     context.Context // cancelled by stop
	cancel  context.CancelFunc
	stopped chan struct{} // closed when the poll goroutine has returned
}

// startAccessFollower starts polling. applied is the key the access rules
// were created with. interval <= 0 means defaultFollowInterval.
func startAccessFollower(applied string, key func() string, apply func(ctx context.Context, key string) error, interval time.Duration) *accessFollower {
	if interval <= 0 {
		interval = defaultFollowInterval
	}
	t := time.NewTicker(interval)
	return startAccessFollowerTicks(applied, key, apply, t.C, t.Stop)
}

// startAccessFollowerTicks is startAccessFollower with the clock injected:
// the loop runs one step per receive on ticks (a nil channel never fires, so
// a test can call step itself) and calls stopTicks, if non-nil, when it exits.
func startAccessFollowerTicks(applied string, key func() string, apply func(ctx context.Context, key string) error, ticks <-chan time.Time, stopTicks func()) *accessFollower {
	ctx, cancel := context.WithCancel(context.Background())
	f := &accessFollower{
		key:     key,
		apply:   apply,
		applied: applied,
		ctx:     ctx,
		cancel:  cancel,
		stopped: make(chan struct{}),
	}
	go f.loop(ticks, stopTicks)
	return f
}

func (f *accessFollower) loop(ticks <-chan time.Time, stopTicks func()) {
	defer close(f.stopped)
	if stopTicks != nil {
		defer stopTicks()
	}
	for {
		select {
		case <-f.ctx.Done():
			return
		case <-ticks:
			f.step()
		}
	}
}

// step is one poll. Only one goroutine may run it at a time: the loop in
// production, the test itself in tests that drive it by hand.
func (f *accessFollower) step() {
	if f.ctx.Err() != nil {
		return
	}
	want := f.key()
	if want == f.applied {
		return
	}
	if err := f.apply(f.ctx, want); err != nil {
		if f.ctx.Err() != nil {
			return // stopping; an aborted apply is not worth a warning
		}
		if text := err.Error(); text != f.lastErr {
			f.lastErr = text
			slog.Warn("localapi: could not update the pipe access for the new console user; the pipe keeps its previous access and the update is retried on every poll",
				"err", err)
		}
		return
	}
	f.applied = want
	f.lastErr = ""
}

// stop ends the poll goroutine. It cancels the context handed to apply, so an
// apply in progress can return early, and does not wait: if apply ignores the
// context the goroutine exits once apply returns. Safe to call more than once.
func (f *accessFollower) stop() {
	f.cancel()
}
