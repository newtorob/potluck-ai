package localapi_test

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signingkey"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// testServer starts a Server over a temporary Unix socket and returns a plain
// net/http client configured to dial that socket, plus a cancel func.
func testServer(t *testing.T, cfg localapi.Config) (*http.Client, context.CancelFunc) {
	t.Helper()

	// Unix sockets cap at 104 chars on macOS / 108 on Linux. t.TempDir()
	// produces paths that include the full test name + a random suffix,
	// which blows past the limit for longer test names. Use /tmp + a short
	// random suffix instead, and clean up with t.Cleanup.
	tmp := fmt.Sprintf("/tmp/potluck-test-%d.sock", time.Now().UnixNano())
	t.Cleanup(func() { _ = os.Remove(tmp) })
	cfg.SocketPath = tmp

	srv := localapi.New(cfg)

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		cancel()
	})

	ready := make(chan struct{})
	go func() {
		// Poll until the socket appears.
		for {
			if _, err := os.Stat(tmp); err == nil {
				close(ready)
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()

	srvErrCh := make(chan error, 1)
	go func() {
		srvErrCh <- srv.Serve(ctx)
	}()

	select {
	case <-ready:
	case err := <-srvErrCh:
		t.Fatalf("server exited before becoming ready: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("server did not start within 5s")
	}

	// http.Client that dials the Unix socket.
	client := &http.Client{
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", tmp)
			},
		},
	}
	return client, cancel
}

// defaultCfg returns a minimal Config with non-nil bus/store/state.
func defaultCfg(t *testing.T) localapi.Config {
	t.Helper()
	return localapi.Config{
		Bus:           events.NewBus(),
		PeerStore:     peerstate.New(),
		State:         localapi.NewState(),
		WGPrivKeyPath: filepath.Join(t.TempDir(), "wg.key"),
		Platform:      "darwin",
	}
}

func get(t *testing.T, client *http.Client, path string) *http.Response {
	t.Helper()
	resp, err := client.Get("http://localhost" + path)
	if err != nil {
		t.Fatalf("GET %s: %v", path, err)
	}
	return resp
}

func checkProtocolHeader(t *testing.T, resp *http.Response) {
	t.Helper()
	v := resp.Header.Get("X-Potluck-Protocol-Version")
	if v != "1" {
		t.Errorf("X-Potluck-Protocol-Version: got %q want %q", v, "1")
	}
}

// ---------------------------------------------------------------------------
// GET /status
// ---------------------------------------------------------------------------

func TestHandleStatus_Starting(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	resp := get(t, client, "/status")
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status: got %d want 200", resp.StatusCode)
	}
	checkProtocolHeader(t, resp)

	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode status: %v", err)
	}
	if body["mesh_status"] != "starting" {
		t.Errorf("mesh_status: got %v want starting", body["mesh_status"])
	}
	if body["peers_online_count"] != float64(0) {
		t.Errorf("peers_online_count: got %v want 0", body["peers_online_count"])
	}
	if body["version"] != "0.1.0" {
		t.Errorf("version: got %v want 0.1.0", body["version"])
	}

	// Readiness probes must always be present (even before the tunnel is up).

	// vpn_conflict: bool field must be present. Its value depends on the test
	// environment; we only assert the key exists and is a bool.
	if _, ok := body["vpn_conflict"]; !ok {
		t.Error("vpn_conflict field missing from /status response")
	}
	if _, ok := body["vpn_conflict"].(bool); !ok {
		t.Errorf("vpn_conflict: expected bool, got %T", body["vpn_conflict"])
	}

	// vpn_detail: must be a string (may be empty).
	if _, ok := body["vpn_detail"]; !ok {
		t.Error("vpn_detail field missing from /status response")
	}
	if _, ok := body["vpn_detail"].(string); !ok {
		t.Errorf("vpn_detail: expected string, got %T", body["vpn_detail"])
	}

	// wg_port_listening: bool field must be present. wgPort was not set via
	// SetWGPort in this test, so the probe skips the bind check → false.
	if _, ok := body["wg_port_listening"]; !ok {
		t.Error("wg_port_listening field missing from /status response")
	}
	if v, ok := body["wg_port_listening"].(bool); !ok {
		t.Errorf("wg_port_listening: expected bool, got %T", body["wg_port_listening"])
	} else if v {
		// wgPort=0 means SetWGPort was never called; probe must return false.
		t.Error("wg_port_listening: expected false when wgPort not set, got true")
	}
}

// TestHandleStatus_WGPortListening verifies that wg_port_listening=true when
// the test pre-binds a UDP socket on the configured port (simulating what the
// WireGuard device does) and SetWGPort is called with that port.
func TestHandleStatus_WGPortListening(t *testing.T) {
	// Bind a UDP socket on an ephemeral port to simulate the WG device.
	pc, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Fatalf("pre-bind: %v", err)
	}
	defer pc.Close()

	// Extract the port that was actually allocated.
	boundPort := pc.LocalAddr().(*net.UDPAddr).Port

	cfg := defaultCfg(t)
	cfg.State.SetWGPort(boundPort)
	client, _ := testServer(t, cfg)

	resp := get(t, client, "/status")
	defer resp.Body.Close()

	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}

	if v, ok := body["wg_port_listening"].(bool); !ok || !v {
		t.Errorf("wg_port_listening: expected true (port pre-bound), got %v", body["wg_port_listening"])
	}
}

func TestHandleStatus_Running(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.State.SetWelcome("node-1", "hh-1", "100.64.0.5", "pubkeyABC")

	client, _ := testServer(t, cfg)

	resp := get(t, client, "/status")
	defer resp.Body.Close()

	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["mesh_status"] != "running" {
		t.Errorf("mesh_status: got %v want running", body["mesh_status"])
	}
	if body["mesh_ip"] != "100.64.0.5" {
		t.Errorf("mesh_ip: got %v", body["mesh_ip"])
	}
}

// ---------------------------------------------------------------------------
// GET /peers
// ---------------------------------------------------------------------------

func TestHandlePeers_Empty(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	resp := get(t, client, "/peers")
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("peers: got %d want 200", resp.StatusCode)
	}
	checkProtocolHeader(t, resp)

	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	peers, _ := body["peers"].([]any)
	if len(peers) != 0 {
		t.Errorf("expected 0 peers, got %d", len(peers))
	}
}

// ---------------------------------------------------------------------------
// POST /pair — coordinator error path
// ---------------------------------------------------------------------------

func TestHandlePair_CoordinatorError(t *testing.T) {
	// Mock coordinator that always returns 404.
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/mesh/enroll" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"pairing token not found"}`)
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	cfg.CoordClient = coordapi.New(mock.URL)
	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"BADTOKEN12345678901234567890"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("expected 404 forwarded from coordinator, got %d", resp.StatusCode)
	}
	checkProtocolHeader(t, resp)

	var errBody map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&errBody); err != nil {
		t.Fatalf("decode error body: %v", err)
	}
	errObj, _ := errBody["error"].(map[string]any)
	if errObj == nil {
		t.Fatalf("expected {error:{code,message}}, got %v", errBody)
	}
	if errObj["code"] != "coordinator_error" {
		t.Errorf("code: got %v want coordinator_error", errObj["code"])
	}
}

func TestHandlePair_AlreadyEnrolled(t *testing.T) {
	cfg := defaultCfg(t)

	// Pre-create the key file so the server marks itself as enrolled at start.
	keyPath := cfg.WGPrivKeyPath
	if err := os.MkdirAll(filepath.Dir(keyPath), 0700); err != nil {
		t.Fatal(err)
	}
	// Write a dummy base64 value (not a real WG key — just to trigger the file-exists check).
	if err := os.WriteFile(keyPath, []byte("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="), 0600); err != nil {
		t.Fatal(err)
	}

	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"SOMETOKEN12345678901234567890123"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusConflict {
		t.Errorf("expected 409, got %d", resp.StatusCode)
	}

	var errBody map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&errBody); err != nil {
		t.Fatalf("decode: %v", err)
	}
	errObj, _ := errBody["error"].(map[string]any)
	if errObj["code"] != "already_enrolled" {
		t.Errorf("code: got %v want already_enrolled", errObj["code"])
	}
}

func TestHandlePair_MissingToken(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":""}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("expected 400, got %d", resp.StatusCode)
	}
}

// ---------------------------------------------------------------------------
// POST /unpair
// ---------------------------------------------------------------------------

func TestHandleUnpair(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	resp, err := client.Post("http://localhost/unpair", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /unpair: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNoContent {
		t.Errorf("expected 204, got %d", resp.StatusCode)
	}
	checkProtocolHeader(t, resp)
}

// ---------------------------------------------------------------------------
// WS /events
// ---------------------------------------------------------------------------

func TestHandleEvents_ReceivesPublishedEvent(t *testing.T) {
	cfg := defaultCfg(t)
	bus := cfg.Bus
	// Same /tmp socket-path scheme as testServer — t.TempDir produces paths
	// longer than the macOS sun_path 104-byte limit for some test names.
	tmp := fmt.Sprintf("/tmp/potluck-test-%d.sock", time.Now().UnixNano())
	t.Cleanup(func() { _ = os.Remove(tmp) })
	cfg.SocketPath = tmp

	srv := localapi.New(cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Wait for socket to appear.
	ready := make(chan struct{})
	go func() {
		for {
			if _, err := os.Stat(tmp); err == nil {
				close(ready)
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	}()
	srvErrCh := make(chan error, 1)
	go func() { srvErrCh <- srv.Serve(ctx) }()

	select {
	case <-ready:
	case err := <-srvErrCh:
		t.Fatalf("server exited before becoming ready: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("server did not start")
	}

	// Dial WS over the Unix socket.
	wsDialer := websocket.DialOptions{
		HTTPClient: &http.Client{
			Transport: &http.Transport{
				DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
					return (&net.Dialer{}).DialContext(ctx, "unix", tmp)
				},
			},
		},
	}

	wsCtx, wsCancel := context.WithTimeout(ctx, 5*time.Second)
	defer wsCancel()

	conn, _, err := websocket.Dial(wsCtx, "ws://localhost/events", &wsDialer)
	if err != nil {
		t.Fatalf("WS dial: %v", err)
	}
	defer conn.CloseNow() //nolint:errcheck

	// Give the server a moment to register the WS subscriber.
	time.Sleep(50 * time.Millisecond)

	// Publish an event.
	bus.Publish(events.NewStatusChanged("running"))

	// Read from the WS.
	var evt events.Event
	readCtx, readCancel := context.WithTimeout(ctx, 3*time.Second)
	defer readCancel()

	if err := wsjson.Read(readCtx, conn, &evt); err != nil {
		t.Fatalf("WS read: %v", err)
	}
	if evt.Type != events.TypeStatusChanged {
		t.Errorf("event type: got %q want %q", evt.Type, events.TypeStatusChanged)
	}
	if evt.Status != "running" {
		t.Errorf("event status: got %q want running", evt.Status)
	}
}

// ---------------------------------------------------------------------------
// Coordinator 5xx → 502 remapping
// ---------------------------------------------------------------------------

func TestHandlePair_CoordinatorServerError_MappedTo502(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, `{"detail":"internal error"}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	cfg.CoordClient = coordapi.New(mock.URL)
	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"BADTOKEN12345678901234567890"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)

	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("expected 502 for coordinator 500, got %d", resp.StatusCode)
	}
}

// ---------------------------------------------------------------------------
// POST /pair — Ed25519 signing-key registration (DERP-token PoP)
// ---------------------------------------------------------------------------

// TestHandlePair_Success_RegistersSigningKey exercises the full happy path:
// a successful /pair must (1) attach signing_public_key to the coordinator's
// EnrollRequest, using the same key this node loads/generates locally, and
// (2) mark that exact key as registered locally ONLY after the coordinator
// actually accepted it — this is the ordering guard from
// internal/signingkey's package doc, verified end-to-end through the real
// HTTP handler rather than just the helper function.
func TestHandlePair_Success_RegistersSigningKey(t *testing.T) {
	var capturedBody map[string]any
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mesh/enroll" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("decode enroll request body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, `{
			"node_id": "11111111-1111-1111-1111-111111111111",
			"household_id": "22222222-2222-2222-2222-222222222222",
			"mesh_ip": "100.64.0.7",
			"mesh_cidr": "100.64.0.0/24",
			"peers": []
		}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	cfg.CoordClient = coordapi.New(mock.URL)
	signingKeyPath := filepath.Join(t.TempDir(), "signing.key")
	cfg.SigningKeyPath = signingKeyPath

	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"GOODTOKEN1234567890123456789012"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 201, got %d: %s", resp.StatusCode, b)
	}

	// The enroll request sent to the coordinator must carry signing_public_key
	// under exactly that field name (coordinator/schemas/mesh.py::EnrollRequest).
	spk, ok := capturedBody["signing_public_key"].(string)
	if !ok || spk == "" {
		t.Fatalf("enroll request did not carry signing_public_key: %v", capturedBody)
	}

	// The node's own local signing key (loaded/generated during this /pair
	// call) must be the exact one sent to the coordinator...
	kp, err := signingkey.LoadOrGenerate(signingKeyPath)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if kp.PublicKeyB64 != spk {
		t.Fatalf("local signing key %q does not match the one sent to the coordinator %q", kp.PublicKeyB64, spk)
	}
	// ...and, because the coordinator returned 201, it must now be recorded
	// as registered so future DERP-token requests are safe to sign.
	if !signingkey.IsRegistered(signingKeyPath, kp.PublicKeyB64) {
		t.Error("expected the signing key to be marked registered after a successful enroll")
	}
}

// TestHandlePair_CoordinatorError_DoesNotRegisterSigningKey is the negative
// half of the ordering guard: if the coordinator REJECTS the enroll call, the
// signing key must NOT be marked registered — signing before confirmed
// registration is exactly the footgun this feature exists to avoid (a
// signed-but-unregistered DERP-token request 401s outright).
func TestHandlePair_CoordinatorError_DoesNotRegisterSigningKey(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"pairing token not found"}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	cfg.CoordClient = coordapi.New(mock.URL)
	signingKeyPath := filepath.Join(t.TempDir(), "signing.key")
	cfg.SigningKeyPath = signingKeyPath

	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"BADTOKEN12345678901234567890"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body) //nolint:errcheck

	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("expected 404 forwarded from coordinator, got %d", resp.StatusCode)
	}

	kp, err := signingkey.LoadOrGenerate(signingKeyPath)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if signingkey.IsRegistered(signingKeyPath, kp.PublicKeyB64) {
		t.Error("signing key must NOT be marked registered when the coordinator rejected enrollment")
	}
}

// ---------------------------------------------------------------------------
// POST /unpair — signing-key + marker cleanup
// ---------------------------------------------------------------------------

func TestHandleUnpair_ClearsSigningKeyAndMarker(t *testing.T) {
	cfg := defaultCfg(t)
	signingKeyPath := filepath.Join(t.TempDir(), "signing.key")
	cfg.SigningKeyPath = signingKeyPath

	kp, err := signingkey.LoadOrGenerate(signingKeyPath)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if err := signingkey.MarkRegistered(signingKeyPath, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}

	client, _ := testServer(t, cfg)

	resp, err := client.Post("http://localhost/unpair", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /unpair: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", resp.StatusCode)
	}

	if _, err := os.Stat(signingKeyPath); !os.IsNotExist(err) {
		t.Errorf("expected signing key file to be removed, stat err=%v", err)
	}
	if signingkey.IsRegistered(signingKeyPath, kp.PublicKeyB64) {
		t.Error("expected registration marker to be cleared after unpair")
	}
}
