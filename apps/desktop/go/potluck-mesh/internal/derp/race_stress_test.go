//go:build race_stress

// Stress tests intended to be run with `go test -race -tags=race_stress`.
// They hammer the concurrent Send / reconnect / Close paths to surface any
// remaining data races. Skipped under the normal short test runs because
// they take 1–2 seconds wall time each.
package derp_test

import (
	"context"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
)

// TestStressSendDuringReconnect spins up the client against a mock relay that
// closes connections every ~50ms, while N goroutines hammer Send() in a loop.
// With -race, any unsynchronised access to sendCh across runOnce reconnects
// would surface here.
func TestStressSendDuringReconnect(t *testing.T) {
	var connCount atomic.Int32
	mr := newMockRelay(t, func(conn net.Conn) {
		connCount.Add(1)
		// Hold connection for a brief, randomized window so the client
		// experiences repeated reconnect cycles during the stress window.
		time.Sleep(50 * time.Millisecond)
	})

	var ourKey [32]byte
	client := derp.New(mr.addr, func(ctx context.Context) (string, error) {
		return "tok", nil
	}, ourKey, func(_ [32]byte, _ []byte) {})

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	runDone := make(chan struct{})
	go func() {
		_ = client.Run(ctx)
		close(runDone)
	}()

	// Fan out N goroutines calling Send() as fast as they can.
	const senderCount = 16
	stopAt := time.Now().Add(1500 * time.Millisecond)
	var wg sync.WaitGroup
	wg.Add(senderCount)
	var sendCalls atomic.Int64
	for g := 0; g < senderCount; g++ {
		go func() {
			defer wg.Done()
			var destKey [32]byte
			destKey[0] = byte(g)
			payload := []byte("stress")
			for time.Now().Before(stopAt) {
				_ = client.Send(destKey, payload)
				sendCalls.Add(1)
			}
		}()
	}
	wg.Wait()
	cancel()
	<-runDone

	t.Logf("stress: %d send calls across %d reconnect cycles",
		sendCalls.Load(), connCount.Load())
	if connCount.Load() < 2 {
		t.Errorf("expected multiple reconnects, got %d", connCount.Load())
	}
}

// TestStressBindOpenCloseConcurrent hammers Open/Close concurrently to
// surface any race on the closeState swap (H6).
func TestStressBindOpenCloseConcurrent(t *testing.T) {
	fb := newFakeBind()
	c := derp.New("tcp+insecure://127.0.0.1:1",
		func(ctx context.Context) (string, error) { return "", nil },
		[32]byte{}, func(_ [32]byte, _ []byte) {})
	b := derp.NewDerpBind(fb, c)

	stopAt := time.Now().Add(1500 * time.Millisecond)

	var wg sync.WaitGroup
	// Concurrent Opens.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for time.Now().Before(stopAt) {
			_, _, _ = b.Open(0)
		}
	}()
	// Concurrent Closes.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for time.Now().Before(stopAt) {
			_ = b.Close()
		}
	}()
	// Concurrent SetPeerRouting / IsPeerDERPRouted to exercise derpMu.
	wg.Add(1)
	go func() {
		defer wg.Done()
		var key [32]byte
		for time.Now().Before(stopAt) {
			b.SetPeerRouting(key, true)
			_ = b.IsPeerDERPRouted(key)
			b.SetPeerRouting(key, false)
		}
	}()

	wg.Wait()
}
