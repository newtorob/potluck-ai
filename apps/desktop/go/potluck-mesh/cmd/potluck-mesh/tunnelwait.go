package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"syscall"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// Starting while another instance holds the tunnel.
//
// The TUN device has a fixed name (utun9 on macOS, potluck0 on Linux), so a
// second sidecar cannot create it while the first is alive: the kernel answers
// "resource busy". That happens when a service manager starts the new binary
// before the old process is gone, for example launchd after an app update has
// replaced the binary under a daemon that outlived it. Exiting at once made the
// service manager start the process again every few seconds, for as long as the
// other instance lived. Instead the new process waits for the tunnel in place.
//
// Only "resource busy" is waited out. Every other error, a missing privilege
// for one, still ends the process with exit 1 as before.

const (
	// tunnelBusyRetryInterval is how often a busy tunnel is tried again.
	tunnelBusyRetryInterval = 5 * time.Second

	// tunnelBusyMaxAttempts bounds the wait, to about 50 minutes. wireguard-go's
	// Linux CreateTUN does not close /dev/net/tun when TUNSETIFF fails, so every
	// busy attempt leaks a descriptor, and a root service has 1024 by default.
	// The process then exits 1 and the service manager starts a fresh one.
	tunnelBusyMaxAttempts = 600
)

// isTunnelBusy reports whether err is the kernel refusing to create the TUN
// device because another process holds it: EBUSY, or the same wording from a
// layer that dropped the errno ("resource busy" on macOS, "device or resource
// busy" on Linux).
func isTunnelBusy(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, syscall.EBUSY) {
		return true
	}
	return strings.Contains(strings.ToLower(err.Error()), "resource busy")
}

// createTunnelWhenFree calls create until it succeeds or fails with something
// other than a busy tunnel. It logs once, at warn, when it first finds the
// tunnel held, and once at info when it gets through. It returns early with
// ctx's error if the process is told to stop while it waits.
func createTunnelWhenFree(ctx context.Context, create func() (*tunnel.Tunnel, error)) (*tunnel.Tunnel, error) {
	return createTunnelRetrying(ctx, create, systemClock{}, tunnelBusyRetryInterval, tunnelBusyMaxAttempts)
}

func createTunnelRetrying(
	ctx context.Context,
	create func() (*tunnel.Tunnel, error),
	clk clock,
	interval time.Duration,
	maxAttempts int,
) (*tunnel.Tunnel, error) {
	var waitingSince time.Time
	for attempt := 1; ; attempt++ {
		tun, err := create()
		if err == nil {
			if !waitingSince.IsZero() {
				slog.Info("tunnel: the other mesh service let go of the tunnel, continuing",
					"waited", clk.Now().Sub(waitingSince).Round(time.Second))
			}
			return tun, nil
		}
		if !isTunnelBusy(err) {
			return nil, err
		}
		if attempt >= maxAttempts {
			return nil, fmt.Errorf("tunnel still held by another process after %d attempts: %w", attempt, err)
		}
		if waitingSince.IsZero() {
			waitingSince = clk.Now()
			slog.Warn("tunnel: another mesh service still holds the tunnel, waiting for it to exit",
				"retry_every", interval, "err", err)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-clk.After(interval):
		}
	}
}
