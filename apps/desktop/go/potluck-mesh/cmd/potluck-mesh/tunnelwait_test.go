package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

func TestIsTunnelBusy(t *testing.T) {
	busy := []error{
		syscall.EBUSY,
		fmt.Errorf("tunnel: create TUN %q: %w", "utun9", syscall.EBUSY),
		errors.New(`tunnel: create TUN "utun9": resource busy`),
		errors.New(`tunnel: create TUN "potluck0": device or resource busy`),
		errors.New(`tunnel: create TUN "utun9": Resource busy`),
	}
	for _, err := range busy {
		if !isTunnelBusy(err) {
			t.Errorf("isTunnelBusy(%v) = false, want true", err)
		}
	}
	other := []error{
		nil,
		syscall.EPERM,
		syscall.EACCES,
		syscall.EMFILE,
		errors.New(`tunnel: create TUN "utun9": operation not permitted`),
		errors.New("CreateTUN(\"potluck0\") failed; /dev/net/tun does not exist"),
		errors.New("tunnel: apply base config: bind: address already in use"),
		context.Canceled,
	}
	for _, err := range other {
		if isTunnelBusy(err) {
			t.Errorf("isTunnelBusy(%v) = true, want false", err)
		}
	}
}

// captureLog routes slog to a buffer for the test and returns it.
func captureLog(t *testing.T) *lockedBuffer {
	t.Helper()
	buf := &lockedBuffer{}
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(old) })
	return buf
}

type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

var errBusy = fmt.Errorf("tunnel: create TUN %q: %w", "utun9", syscall.EBUSY)

type createResult struct {
	tun *tunnel.Tunnel
	err error
}

// runCreate runs createTunnelRetrying in a goroutine with the given create
// function and returns the channel its result arrives on.
func runCreate(ctx context.Context, clk clock, create func() (*tunnel.Tunnel, error), maxAttempts int) chan createResult {
	out := make(chan createResult, 1)
	go func() {
		tun, err := createTunnelRetrying(ctx, create, clk, tunnelBusyRetryInterval, maxAttempts)
		out <- createResult{tun, err}
	}()
	return out
}

func TestCreateTunnel_FreeTunnelNeedsNoWait(t *testing.T) {
	logs := captureLog(t)
	want := new(tunnel.Tunnel)
	var calls atomic.Int32
	got, err := createTunnelRetrying(context.Background(), func() (*tunnel.Tunnel, error) {
		calls.Add(1)
		return want, nil
	}, newFakeClock(), tunnelBusyRetryInterval, 10)
	if err != nil || got != want {
		t.Fatalf("got (%v, %v), want the tunnel and no error", got, err)
	}
	if calls.Load() != 1 {
		t.Fatalf("create called %d times, want 1", calls.Load())
	}
	if s := logs.String(); strings.Contains(s, "another mesh service") {
		t.Fatalf("logged a wait for a free tunnel:\n%s", s)
	}
}

func TestCreateTunnel_BusyThenFreeRetriesEveryFiveSecondsAndContinues(t *testing.T) {
	logs := captureLog(t)
	clk := newFakeClock()
	want := new(tunnel.Tunnel)
	var calls atomic.Int32
	out := runCreate(context.Background(), clk, func() (*tunnel.Tunnel, error) {
		if calls.Add(1) <= 4 {
			return nil, errBusy
		}
		return want, nil
	}, 100)

	for i := 1; i <= 4; i++ {
		w := clk.wait(t)
		if w.d != 5*time.Second {
			t.Fatalf("wait %d is %v, want 5s", i, w.d)
		}
		if n := calls.Load(); n != int32(i) {
			t.Fatalf("after %d waits create was called %d times", i-1, n)
		}
		clk.release(w)
	}
	res := <-out
	if res.err != nil || res.tun != want {
		t.Fatalf("got (%v, %v), want the tunnel and no error", res.tun, res.err)
	}
	if calls.Load() != 5 {
		t.Fatalf("create called %d times, want 5", calls.Load())
	}

	s := logs.String()
	if n := strings.Count(s, "another mesh service still holds the tunnel"); n != 1 {
		t.Fatalf("the busy warning was logged %d times, want once:\n%s", n, s)
	}
	if !strings.Contains(s, "level=WARN") || !strings.Contains(s, "let go of the tunnel") {
		t.Fatalf("expected one warn and one info line on success:\n%s", s)
	}
}

func TestCreateTunnel_OtherErrorsEndAtOnceAsBefore(t *testing.T) {
	for _, bad := range []error{
		syscall.EPERM,
		errors.New(`tunnel: create TUN "utun9": operation not permitted`),
		errors.New("tunnel: apply base config: boom"),
	} {
		var calls atomic.Int32
		clk := newFakeClock()
		tun, err := createTunnelRetrying(context.Background(), func() (*tunnel.Tunnel, error) {
			calls.Add(1)
			return nil, bad
		}, clk, tunnelBusyRetryInterval, 10)
		if tun != nil || !errors.Is(err, bad) {
			t.Errorf("%v: got (%v, %v), want that error unchanged", bad, tun, err)
		}
		if calls.Load() != 1 {
			t.Errorf("%v: create called %d times, want 1", bad, calls.Load())
		}
		select {
		case <-clk.armed:
			t.Errorf("%v: waited before giving up", bad)
		default:
		}
	}
}

func TestCreateTunnel_ErrorAfterWaitingIsStillReturned(t *testing.T) {
	clk := newFakeClock()
	var calls atomic.Int32
	denied := errors.New("tunnel: create TUN: operation not permitted")
	out := runCreate(context.Background(), clk, func() (*tunnel.Tunnel, error) {
		if calls.Add(1) == 1 {
			return nil, errBusy
		}
		return nil, denied
	}, 10)
	clk.release(clk.wait(t))
	res := <-out
	if res.tun != nil || !errors.Is(res.err, denied) {
		t.Fatalf("got (%v, %v), want the second error", res.tun, res.err)
	}
}

func TestCreateTunnel_StopsWaitingWhenTheProcessIsToldToStop(t *testing.T) {
	clk := newFakeClock()
	ctx, cancel := context.WithCancel(context.Background())
	out := runCreate(ctx, clk, func() (*tunnel.Tunnel, error) { return nil, errBusy }, 100)
	clk.wait(t)
	cancel()
	select {
	case res := <-out:
		if !errors.Is(res.err, context.Canceled) {
			t.Fatalf("got %v, want context.Canceled", res.err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("still waiting after the context was cancelled")
	}
}

func TestCreateTunnel_GivesUpAfterTheAttemptLimit(t *testing.T) {
	clk := newFakeClock()
	var calls atomic.Int32
	out := runCreate(context.Background(), clk, func() (*tunnel.Tunnel, error) {
		calls.Add(1)
		return nil, errBusy
	}, 3)
	clk.release(clk.wait(t))
	clk.release(clk.wait(t))
	res := <-out
	if res.tun != nil || !errors.Is(res.err, syscall.EBUSY) {
		t.Fatalf("got (%v, %v), want an error wrapping EBUSY", res.tun, res.err)
	}
	if !strings.Contains(res.err.Error(), "after 3 attempts") {
		t.Fatalf("error does not say how long it tried: %v", res.err)
	}
	if calls.Load() != 3 {
		t.Fatalf("create called %d times, want 3", calls.Load())
	}
}
