//go:build race_stress

// Stress test for the signal Client's send-channel race during reconnect.
// Run with: go test -race -tags=race_stress ./internal/signal/...
package signal

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"
)

type noopHandler struct{}

func (noopHandler) OnWelcome(Welcome)           {}
func (noopHandler) OnRosterUpdate(RosterUpdate) {}
func (noopHandler) OnPunchCoord(PunchCoord)     {}

// flakyWSServer is a WebSocket server that completes the hello/welcome
// handshake then closes the connection after a short window, forcing the
// client into a reconnect loop.
func flakyWSServer(t *testing.T, holdFor time.Duration, connCount *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close(websocket.StatusInternalError, "test")
		connCount.Add(1)

		// Read hello, respond with welcome.
		var raw json.RawMessage
		readCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := wsjson.Read(readCtx, c, &raw); err != nil {
			return
		}
		welcome := Welcome{
			Type:        "welcome",
			NodeID:      "test-node",
			HouseholdID: "test-hh",
			MeshIP:      "10.0.0.1",
		}
		if err := wsjson.Write(r.Context(), c, welcome); err != nil {
			return
		}

		// Hold connection for the configured window, then drop.
		time.Sleep(holdFor)
	}))
}

// TestStressSignalSendDuringReconnect: many goroutines call SendEndpointUpdate /
// SendRequestPunch while the server forces repeated reconnects. With -race,
// any concurrent read/write on sendCh across runOnce cycles fails the test.
func TestStressSignalSendDuringReconnect(t *testing.T) {
	var connCount atomic.Int32
	srv := flakyWSServer(t, 50*time.Millisecond, &connCount)
	defer srv.Close()

	// Convert http://... to ws://...
	url := "ws" + srv.URL[len("http"):] + "/mesh/signal"

	c := NewClient(url, "test-wg-pubkey", "", noopHandler{})

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	runDone := make(chan struct{})
	go func() {
		_ = c.Run(ctx)
		close(runDone)
	}()

	const senderCount = 16
	stopAt := time.Now().Add(1500 * time.Millisecond)
	var wg sync.WaitGroup
	wg.Add(senderCount)
	var calls atomic.Int64
	for g := 0; g < senderCount; g++ {
		go func(id int) {
			defer wg.Done()
			for time.Now().Before(stopAt) {
				if id%2 == 0 {
					c.SendEndpointUpdate("1.2.3.4:51820")
				} else {
					c.SendRequestPunch("11111111-1111-1111-1111-111111111111")
				}
				calls.Add(1)
			}
		}(g)
	}
	wg.Wait()
	cancel()
	<-runDone

	t.Logf("stress: %d send calls across %d reconnect cycles",
		calls.Load(), connCount.Load())
	if connCount.Load() < 2 {
		t.Errorf("expected multiple reconnects, got %d", connCount.Load())
	}
}
