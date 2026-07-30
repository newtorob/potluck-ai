package signal_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

type noopHandler struct{}

func (noopHandler) OnWelcome(signal.Welcome)           {}
func (noopHandler) OnRosterUpdate(signal.RosterUpdate) {}
func (noopHandler) OnPunchCoord(signal.PunchCoord)     {}

// headerCapture is a thread-safe holder for the most recently observed
// /mesh/signal upgrade request headers — read from the test goroutine,
// written from the httptest server's handler goroutine.
type headerCapture struct {
	mu  sync.Mutex
	hdr http.Header
}

func (h *headerCapture) set(v http.Header) {
	h.mu.Lock()
	h.hdr = v.Clone()
	h.mu.Unlock()
}

func (h *headerCapture) get() http.Header {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.hdr
}

// capturingWSServer records every /mesh/signal upgrade request's headers,
// completes hello/welcome, then briefly holds the connection open.
func capturingWSServer(t *testing.T) (*httptest.Server, *headerCapture) {
	t.Helper()
	cap := &headerCapture{}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cap.set(r.Header)

		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close(websocket.StatusInternalError, "test")

		var raw json.RawMessage
		readCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := wsjson.Read(readCtx, c, &raw); err != nil {
			return
		}
		welcome := signal.Welcome{
			Type:        "welcome",
			NodeID:      "test-node",
			HouseholdID: "test-hh",
			MeshIP:      "10.0.0.1",
		}
		_ = wsjson.Write(r.Context(), c, welcome)
		// Hold briefly so the client settles past OnWelcome before this
		// handler returns and the connection drops.
		time.Sleep(150 * time.Millisecond)
	}))
	return srv, cap
}

// TestDial_AttachesBearerAndLegacyHeaders pins the wire contract: the
// /mesh/signal WS upgrade carries BOTH Authorization: Bearer <node_token>
// (primary, whenever nodeTokenFn returns non-empty) AND
// X-Potluck-Coordinator-Token (legacy, whenever configured) — matching
// coordinator/middleware/node_auth.py's "either credential is accepted,
// attach both when both are available" contract.
func TestDial_AttachesBearerAndLegacyHeaders(t *testing.T) {
	srv, cap := capturingWSServer(t)
	defer srv.Close()

	got := dialAndWaitForHeaders(t, srv, "legacy-secret", func() string { return "node-tok-abc" }, cap)

	if v := got.Get("Authorization"); v != "Bearer node-tok-abc" {
		t.Errorf("Authorization header: got %q want %q", v, "Bearer node-tok-abc")
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "legacy-secret" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q want %q", v, "legacy-secret")
	}
}

// TestDial_NoNodeToken_OmitsBearer pins the pre-enroll case: nodeTokenFn
// returning "" (not yet enrolled) must NOT send an empty/garbage
// Authorization header — it must be entirely absent, falling back to
// whatever legacy coordinator token is configured (possibly also empty in
// full dev mode, in which case neither header is sent at all).
func TestDial_NoNodeToken_OmitsBearer(t *testing.T) {
	srv, cap := capturingWSServer(t)
	defer srv.Close()

	got := dialAndWaitForHeaders(t, srv, "", func() string { return "" }, cap)

	if v := got.Get("Authorization"); v != "" {
		t.Errorf("Authorization header: got %q, want absent", v)
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q, want absent", v)
	}
}

// TestDial_NilNodeTokenFn_LegacyOnly pins back-compat: a nil nodeTokenFn
// (e.g. a caller that hasn't wired node-token support at all) must not panic
// and must still attach the legacy header when configured.
func TestDial_NilNodeTokenFn_LegacyOnly(t *testing.T) {
	srv, cap := capturingWSServer(t)
	defer srv.Close()

	got := dialAndWaitForHeaders(t, srv, "legacy-secret", nil, cap)

	if v := got.Get("Authorization"); v != "" {
		t.Errorf("Authorization header: got %q, want absent (nil nodeTokenFn)", v)
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "legacy-secret" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q want %q", v, "legacy-secret")
	}
}

// dialAndWaitForHeaders dials once and polls cap until the server has
// recorded an upgrade request, then tears the client down.
func dialAndWaitForHeaders(t *testing.T, srv *httptest.Server, coordinatorToken string, nodeTokenFn func() string, cap *headerCapture) http.Header {
	t.Helper()
	url := "ws" + srv.URL[len("http"):] + "/mesh/signal"
	c := signal.NewClient(url, "test-wg-pubkey", coordinatorToken, nodeTokenFn, noopHandler{})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = c.Run(ctx)
		close(done)
	}()

	deadline := time.Now().Add(3 * time.Second)
	var hdr http.Header
	for time.Now().Before(deadline) {
		if hdr = cap.get(); hdr != nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	<-done

	if hdr == nil {
		t.Fatal("server never captured an upgrade request within the deadline")
	}
	return hdr
}

// ---------------------------------------------------------------------------
// SignalURL / SetSignalURL — runtime coordinator-URL override support
// ---------------------------------------------------------------------------

func TestClient_SignalURL_ReturnsConstructorValue(t *testing.T) {
	c := signal.NewClient("ws://127.0.0.1:8400/mesh/signal", "pubkey", "", nil, noopHandler{})
	if got := c.SignalURL(); got != "ws://127.0.0.1:8400/mesh/signal" {
		t.Errorf("SignalURL() = %q, want %q", got, "ws://127.0.0.1:8400/mesh/signal")
	}
}

func TestClient_SetSignalURL_UpdatesLiveValue(t *testing.T) {
	c := signal.NewClient("ws://127.0.0.1:8400/mesh/signal", "pubkey", "", nil, noopHandler{})
	c.SetSignalURL("wss://coordinator.example.com/mesh/signal")
	if got := c.SignalURL(); got != "wss://coordinator.example.com/mesh/signal" {
		t.Errorf("SignalURL() after SetSignalURL = %q, want %q", got, "wss://coordinator.example.com/mesh/signal")
	}
}

// TestClient_SetSignalURL_ConcurrentWithRun is the concurrency-safety
// regression test for the runtime coordinator-URL override path
// (cmd/potluck-mesh/main.go's applyCoordinatorURL calls SetSignalURL while
// Run's connect loop is independently reading the URL on every reconnect
// attempt). Run under -race; a bare string field would fail this test.
func TestClient_SetSignalURL_ConcurrentWithRun(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck
		// Never send a welcome — the dial loop will time out on helloTimeout
		// and retry, giving SetSignalURL plenty of concurrent opportunities.
		<-r.Context().Done()
	}))
	defer srv.Close()
	wsURL := "ws" + srv.URL[len("http"):]

	c := signal.NewClient(wsURL, "pubkey", "", nil, noopHandler{})

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = c.Run(ctx)
	}()

	for i := 0; i < 50; i++ {
		c.SetSignalURL(wsURL)
	}

	<-done
}

// ---------------------------------------------------------------------------
// WGPublicKey / SetWGPublicKey — live-key propagation after POST /pair
// ---------------------------------------------------------------------------
//
// Regression coverage for the bug where a /pair on an already-running daemon
// (started unenrolled, so already mid-backoff-loop hello-ing with "") never
// picked up the freshly-enrolled key until the whole process restarted —
// client.wgPublicKey was captured once at NewClient time with no update path.

func TestClient_WGPublicKey_ReturnsConstructorValue(t *testing.T) {
	c := signal.NewClient("ws://127.0.0.1:8400/mesh/signal", "pubkey", "", nil, noopHandler{})
	if got := c.WGPublicKey(); got != "pubkey" {
		t.Errorf("WGPublicKey() = %q, want %q", got, "pubkey")
	}
}

func TestClient_SetWGPublicKey_UpdatesLiveValue(t *testing.T) {
	c := signal.NewClient("ws://127.0.0.1:8400/mesh/signal", "pubkey", "", nil, noopHandler{})
	c.SetWGPublicKey("fresh-pubkey-from-pair")
	if got := c.WGPublicKey(); got != "fresh-pubkey-from-pair" {
		t.Errorf("WGPublicKey() after SetWGPublicKey = %q, want %q", got, "fresh-pubkey-from-pair")
	}
}

// TestClient_SetWGPublicKey_ConcurrentWithRun mirrors
// TestClient_SetSignalURL_ConcurrentWithRun — run under -race.
func TestClient_SetWGPublicKey_ConcurrentWithRun(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow() //nolint:errcheck
		// Never send a welcome — the dial loop will time out on helloTimeout
		// and retry, giving SetWGPublicKey plenty of concurrent opportunities.
		<-r.Context().Done()
	}))
	defer srv.Close()
	wsURL := "ws" + srv.URL[len("http"):]

	c := signal.NewClient(wsURL, "pubkey", "", nil, noopHandler{})

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = c.Run(ctx)
	}()

	for i := 0; i < 50; i++ {
		c.SetWGPublicKey("pubkey-iter")
	}

	<-done
}

// helloKeyCapture records the wg_public_key carried on every hello the
// server observes, in order.
type helloKeyCapture struct {
	mu   sync.Mutex
	keys []string
}

func (h *helloKeyCapture) add(k string) {
	h.mu.Lock()
	h.keys = append(h.keys, k)
	h.mu.Unlock()
}

func (h *helloKeyCapture) snapshot() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.keys...)
}

// TestClient_SetWGPublicKey_PropagatesToNextHello is the end-to-end
// regression test for the /pair-without-restart bug: it dials once with an
// initial key, calls SetWGPublicKey (as main.go's signalRestart now does
// immediately after a successful /pair), and asserts the NEXT reconnect's
// hello carries the updated key — all without recreating the Client or
// restarting the process. The server never completes the welcome handshake,
// so the client's own backoff/retry loop drives the reconnect.
func TestClient_SetWGPublicKey_PropagatesToNextHello(t *testing.T) {
	hc := &helloKeyCapture{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck

		var hello struct {
			WGPublicKey string `json:"wg_public_key"`
		}
		readCtx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := wsjson.Read(readCtx, c, &hello); err != nil {
			return
		}
		hc.add(hello.WGPublicKey)
		// Deliberately never send a welcome — closes immediately (via the
		// deferred CloseNow above) so the client's backoff loop retries.
	}))
	defer srv.Close()
	wsURL := "ws" + srv.URL[len("http"):] + "/mesh/signal"

	c := signal.NewClient(wsURL, "pubkey-v1-pre-enroll", "", nil, noopHandler{})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = c.Run(ctx)
		close(done)
	}()

	// Wait for the first hello attempt.
	waitFor(t, 3*time.Second, func() bool { return len(hc.snapshot()) >= 1 })

	// Simulate /pair completing on the running daemon: propagate the freshly
	// minted key to the live client BEFORE the next reconnect fires.
	c.SetWGPublicKey("pubkey-v2-post-pair")

	// Wait for a subsequent hello driven by the client's own retry loop.
	waitFor(t, 5*time.Second, func() bool { return len(hc.snapshot()) >= 2 })

	cancel()
	<-done

	keys := hc.snapshot()
	if len(keys) < 2 {
		t.Fatalf("expected at least 2 hello attempts, got %d: %v", len(keys), keys)
	}
	if keys[0] != "pubkey-v1-pre-enroll" {
		t.Errorf("first hello: got %q want %q", keys[0], "pubkey-v1-pre-enroll")
	}
	if last := keys[len(keys)-1]; last != "pubkey-v2-post-pair" {
		t.Errorf("hello after SetWGPublicKey: got %q want %q (client is still hello-ing with the stale key — the /pair-without-restart bug)", last, "pubkey-v2-post-pair")
	}
}

// waitFor polls cond every 10ms until it returns true or timeout elapses,
// failing the test on timeout.
func waitFor(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("condition not met within %s", timeout)
}
