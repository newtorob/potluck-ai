package localapi_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/runtimesettings"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
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

// testKey returns a canonical base64 WireGuard public key (32 bytes of seed).
// The peer store refuses roster entries whose key is not one, so the roster
// fixtures below use these rather than free-form strings.
func testKey(seed byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = seed
	}
	return base64.StdEncoding.EncodeToString(b)
}

var (
	keyABC        = testKey(0xA1)
	keyXYZ        = testKey(0xA2)
	keyConnecting = testKey(0xA3)
	keyDirect     = testKey(0xA4)
)

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

// patch issues a PATCH request with a JSON body. http.Client has no Patch
// convenience method (unlike Get/Post), so build the request by hand.
func patch(t *testing.T, client *http.Client, path, jsonBody string) *http.Response {
	t.Helper()
	req, err := http.NewRequest(http.MethodPatch, "http://localhost"+path, strings.NewReader(jsonBody))
	if err != nil {
		t.Fatalf("build PATCH %s request: %v", path, err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("PATCH %s: %v", path, err)
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

// TestHandleStatus_ExeSHA256 pins exe_sha256: the hash main.go records at
// startup is reported verbatim, and a sidecar that could not compute one still
// sends the key, as an empty string (an older sidecar has no such key at all).
func TestHandleStatus_ExeSHA256(t *testing.T) {
	const sum = "3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b"

	decode := func(cfg localapi.Config) map[string]any {
		t.Helper()
		client, _ := testServer(t, cfg)
		resp := get(t, client, "/status")
		defer resp.Body.Close()
		var body map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		return body
	}

	cfg := defaultCfg(t)
	cfg.State.SetExeSHA256(sum)
	if got := decode(cfg)["exe_sha256"]; got != sum {
		t.Errorf("exe_sha256: got %v want %s", got, sum)
	}

	unset := decode(defaultCfg(t))
	got, ok := unset["exe_sha256"]
	if !ok {
		t.Fatal("exe_sha256 key missing when no hash was recorded")
	}
	if got != "" {
		t.Errorf("exe_sha256 with no hash recorded: got %v want empty string", got)
	}
}

// ---------------------------------------------------------------------------
// POST /restart
// ---------------------------------------------------------------------------

// TestRestart_AnswersAcceptedThenRunsTheRestart pins the contract the desktop
// app relies on: 202 first, then the registered restart runs (it ends the
// process, so it must not run before the response is written).
func TestRestart_AnswersAcceptedThenRunsTheRestart(t *testing.T) {
	cfg := defaultCfg(t)
	ran := make(chan struct{}, 2)
	cfg.State.SetRestartFunc(func() { ran <- struct{}{} })
	client, _ := testServer(t, cfg)

	resp, err := client.Post("http://localhost/restart", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /restart: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status: got %d want 202", resp.StatusCode)
	}
	checkProtocolHeader(t, resp)
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["restarting"] != true {
		t.Errorf("restarting: got %v want true", body["restarting"])
	}

	select {
	case <-ran:
	case <-time.After(5 * time.Second):
		t.Fatal("the restart did not run after the 202")
	}
}

func TestRestart_NotImplementedWithoutARestartFunc(t *testing.T) {
	client, _ := testServer(t, defaultCfg(t))

	resp, err := client.Post("http://localhost/restart", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /restart: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("status: got %d want 501", resp.StatusCode)
	}
	var body map[string]map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["error"]["code"] != "restart_unavailable" {
		t.Errorf("error code: got %v want restart_unavailable", body["error"]["code"])
	}
}

func TestRestart_OnlyPOST(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.State.SetRestartFunc(func() { t.Error("restart ran for a GET") })
	client, _ := testServer(t, cfg)

	resp := get(t, client, "/restart")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /restart: got %d want 405", resp.StatusCode)
	}
}

// TestHandleStatus_RestartFeature: /status lists restart_v1 exactly when POST
// /restart is available, next to whatever other features were set, so the app
// can tell a sidecar that has the command from one that only reports its hash.
func TestHandleStatus_RestartFeature(t *testing.T) {
	features := func(cfg localapi.Config) []any {
		t.Helper()
		client, _ := testServer(t, cfg)
		resp := get(t, client, "/status")
		defer resp.Body.Close()
		var body map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		list, ok := body["features"].([]any)
		if !ok {
			t.Fatalf("features: got %#v, want a list", body["features"])
		}
		return list
	}

	if got := features(defaultCfg(t)); len(got) != 0 {
		t.Errorf("features without a restart command: %v, want none", got)
	}

	cfg := defaultCfg(t)
	cfg.State.SetFeatures([]string{"circle_link_v1"})
	cfg.State.SetRestartFunc(func() {})
	got := features(cfg)
	if len(got) != 2 || got[0] != "circle_link_v1" || got[1] != localapi.FeatureRestartV1 {
		t.Errorf("features: got %v, want [circle_link_v1 %s]", got, localapi.FeatureRestartV1)
	}
	if localapi.FeatureRestartV1 != "restart_v1" {
		t.Errorf("feature name changed to %q; the desktop app looks for restart_v1", localapi.FeatureRestartV1)
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

// TestHandlePeers_DERPFlippedNotYetReachable_ReportsConnecting verifies that
// a peer that this node has flipped to DERP routing but
// that has not yet completed a WireGuard handshake must report
// reachability_mode "connecting" and derp_flipped: true - not a null
// reachability_mode indistinguishable from "never tried DERP".
func TestHandlePeers_DERPFlippedNotYetReachable_ReportsConnecting(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.PeerStore.Upsert(signal.RosterPeer{
		NodeID:      "peer-1",
		NodeName:    "linux-box",
		MeshIP:      "100.64.0.7",
		WGPublicKey: keyABC,
	})
	// PeerReachFn returns a zero time (no handshake yet).
	cfg.PeerReachFn = func(pubkeyBase64 string) (time.Time, uint64, uint64, error) {
		return time.Time{}, 0, 0, nil
	}
	cfg.IsDERPRoutedFn = func(pubkeyBase64 string) bool {
		return pubkeyBase64 == keyABC
	}

	client, _ := testServer(t, cfg)

	resp := get(t, client, "/peers")
	defer resp.Body.Close()

	var body struct {
		Peers []map[string]any `json:"peers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Peers) != 1 {
		t.Fatalf("expected 1 peer, got %d", len(body.Peers))
	}
	p := body.Peers[0]

	if reachable, _ := p["reachable"].(bool); reachable {
		t.Error("reachable: expected false (no handshake yet)")
	}
	if flipped, ok := p["derp_flipped"].(bool); !ok || !flipped {
		t.Errorf("derp_flipped: expected true, got %v", p["derp_flipped"])
	}
	if mode, _ := p["reachability_mode"].(string); mode != "connecting" {
		t.Errorf("reachability_mode: got %v want \"connecting\"", p["reachability_mode"])
	}
}

// TestHandlePeers_NotFlippedNotReachable_ReportsNullMode is the control case:
// a peer that was never DERP-flipped and has no handshake still reports
// reachability_mode: null and derp_flipped: false - unchanged from prior
// behavior, so the new "connecting" state doesn't leak onto peers that are
// simply still trying direct.
func TestHandlePeers_NotFlippedNotReachable_ReportsNullMode(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.PeerStore.Upsert(signal.RosterPeer{
		NodeID:      "peer-1",
		NodeName:    "windows-box",
		MeshIP:      "100.64.0.8",
		WGPublicKey: keyXYZ,
	})
	cfg.PeerReachFn = func(pubkeyBase64 string) (time.Time, uint64, uint64, error) {
		return time.Time{}, 0, 0, nil
	}
	cfg.IsDERPRoutedFn = func(pubkeyBase64 string) bool { return false }

	client, _ := testServer(t, cfg)

	resp := get(t, client, "/peers")
	defer resp.Body.Close()

	var body struct {
		Peers []map[string]any `json:"peers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	p := body.Peers[0]

	if flipped, ok := p["derp_flipped"].(bool); !ok || flipped {
		t.Errorf("derp_flipped: expected false, got %v", p["derp_flipped"])
	}
	if _, present := p["reachability_mode"]; !present {
		t.Fatal("reachability_mode key missing from response")
	}
	if p["reachability_mode"] != nil {
		t.Errorf("reachability_mode: got %v want null", p["reachability_mode"])
	}
}

// TestHandlePeers_EveryEntryCarriesHouseholdScope checks the scope tag on each
// entry, whatever its reachability. The engine counts a peer as one of the
// owner's machines only when the tag is absent or "household", so the tag has
// to be present and exact on every entry this sidecar lists.
func TestHandlePeers_EveryEntryCarriesHouseholdScope(t *testing.T) {
	cfg := defaultCfg(t)
	for i, ip := range []string{"100.64.0.7", "100.64.0.8", "100.64.0.9"} {
		cfg.PeerStore.Upsert(signal.RosterPeer{
			NodeID:      fmt.Sprintf("peer-%d", i),
			NodeName:    fmt.Sprintf("box-%d", i),
			MeshIP:      ip,
			WGPublicKey: testKey(byte(i + 1)),
		})
	}
	// One peer with a fresh handshake, one flipped to DERP with none, one idle.
	fresh := testKey(1)
	flipped := testKey(2)
	cfg.PeerReachFn = func(pubkeyBase64 string) (time.Time, uint64, uint64, error) {
		if pubkeyBase64 == fresh {
			return time.Now(), 0, 0, nil
		}
		return time.Time{}, 0, 0, nil
	}
	cfg.IsDERPRoutedFn = func(pubkeyBase64 string) bool { return pubkeyBase64 == flipped }

	client, _ := testServer(t, cfg)
	resp := get(t, client, "/peers")
	defer resp.Body.Close()

	var body struct {
		Peers []map[string]json.RawMessage `json:"peers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.Peers) != 3 {
		t.Fatalf("expected 3 peers, got %d", len(body.Peers))
	}
	for _, p := range body.Peers {
		if got := string(p["scope"]); got != `"household"` {
			t.Errorf("%s: scope = %s, want \"household\"", p["node_name"], got)
		}
	}
}

// TestHandlePeers_NeverListsRejectedEntries feeds the peer store entries the
// gate must refuse (a gateway address, this node's own address, two nodes on one
// address, a malformed key, a hostile mesh_ip) beside good ones, and checks that
// GET /peers and the /status peer count only ever reflect the good ones, including
// after a listed peer's entry turns invalid or is claimed by another node.
func TestHandlePeers_NeverListsRejectedEntries(t *testing.T) {
	cfg := defaultCfg(t)
	mk := func(id, ip string, seed byte) signal.RosterPeer {
		return signal.RosterPeer{NodeID: id, NodeName: id, MeshIP: ip, WGPublicKey: testKey(seed)}
	}
	badKey := mk("bad-key", "100.64.0.10", 7)
	badKey.WGPublicKey = "peerPubKeyABC"
	cfg.PeerStore.Reset("100.64.0.6", []signal.RosterPeer{
		mk("linux", "100.64.0.7", 1),
		mk("gateway", "192.168.1.1", 2),
		mk("windows", "100.64.0.8", 3),
		mk("self-twin", "100.64.0.6", 4),
		mk("dup-a", "100.64.0.9", 5),
		mk("dup-b", "100.64.0.9", 6),
		badKey,
		mk("hostile", "100.64.0.11\nallowed_ip=0.0.0.0/0", 8),
		mk("prefixed", "100.64.0.12/32", 9),
		mk("port", "100.64.0.13:51820", 10),
	})

	client, _ := testServer(t, cfg)
	listed := func() []string {
		t.Helper()
		resp := get(t, client, "/peers")
		defer resp.Body.Close()
		var body struct {
			Peers []struct {
				NodeID string `json:"node_id"`
				MeshIP string `json:"mesh_ip"`
			} `json:"peers"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		var out []string
		for _, p := range body.Peers {
			out = append(out, p.NodeID+"@"+p.MeshIP)
		}
		sort.Strings(out)
		return out
	}
	statusCount := func() float64 {
		t.Helper()
		resp := get(t, client, "/status")
		defer resp.Body.Close()
		var body map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode: %v", err)
		}
		return body["peers_online_count"].(float64)
	}
	want := func(what string, got []string, want ...string) {
		t.Helper()
		if strings.Join(got, ",") != strings.Join(want, ",") {
			t.Errorf("%s: /peers = %v, want %v", what, got, want)
		}
	}

	want("mixed roster", listed(), "linux@100.64.0.7", "windows@100.64.0.8")
	if n := statusCount(); n != 2 {
		t.Errorf("peers_online_count = %v, want 2", n)
	}

	// A listed peer's entry turns invalid.
	_, _, evict, _ := cfg.PeerStore.Merge(nil, []signal.RosterPeer{mk("linux", "8.8.8.8", 1)})
	for _, id := range evict {
		cfg.PeerStore.Remove(id)
	}
	want("after linux turns invalid", listed(), "windows@100.64.0.8")

	// Another node claims a listed peer's address: neither is listed.
	_, _, evict, _ = cfg.PeerStore.Merge([]signal.RosterPeer{mk("thief", "100.64.0.8", 11)}, nil)
	for _, id := range evict {
		cfg.PeerStore.Remove(id)
	}
	want("after a claim on windows", listed())
	if n := statusCount(); n != 0 {
		t.Errorf("peers_online_count = %v, want 0", n)
	}
}

// TestHandleStatus_PeersConnectingCount verifies the node-level aggregate:
// GET /status surfaces peers_connecting_count so a systray/global indicator
// can observe the same "flipped to DERP, handshake pending" state without
// walking the /peers list.
func TestHandleStatus_PeersConnectingCount(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.PeerStore.Upsert(signal.RosterPeer{
		NodeID:      "peer-1",
		NodeName:    "linux-box",
		MeshIP:      "100.64.0.7",
		WGPublicKey: keyConnecting,
	})
	cfg.PeerStore.Upsert(signal.RosterPeer{
		NodeID:      "peer-2",
		NodeName:    "windows-box",
		MeshIP:      "100.64.0.8",
		WGPublicKey: keyDirect,
	})
	cfg.PeerReachFn = func(pubkeyBase64 string) (time.Time, uint64, uint64, error) {
		if pubkeyBase64 == keyDirect {
			return time.Now(), 0, 0, nil
		}
		return time.Time{}, 0, 0, nil
	}
	cfg.IsDERPRoutedFn = func(pubkeyBase64 string) bool {
		return pubkeyBase64 == keyConnecting
	}

	client, _ := testServer(t, cfg)

	resp := get(t, client, "/status")
	defer resp.Body.Close()

	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["peers_connecting_count"] != float64(1) {
		t.Errorf("peers_connecting_count: got %v want 1", body["peers_connecting_count"])
	}
}

// ---------------------------------------------------------------------------
// POST /pair - coordinator error path
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
	// Write a dummy base64 value (not a real WG key - just to trigger the file-exists check).
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

// TestHandlePair_FreshKeyAloneIsNotEnrolled covers the "fresh machine 409s on
// its first /pair" case: ensureKeypair generates a
// wg.key unconditionally at tunnel bring-up, before /pair is ever called, so
// a never-paired machine already has a key file on disk. When NodeTokenPath
// is configured (the production wiring - see main.go), enrolled-detection
// must key off the persisted node_token, not mere key-file existence, or
// POST /pair 409s with already_enrolled on a machine that has never actually
// enrolled - the in-app "Add a machine" flow would never get past the first
// attempt.
func TestHandlePair_FreshKeyAloneIsNotEnrolled(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mesh/enroll" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, `{
			"node_id": "11111111-1111-1111-1111-111111111111",
			"household_id": "22222222-2222-2222-2222-222222222222",
			"mesh_ip": "100.64.0.7",
			"mesh_cidr": "100.64.0.0/24",
			"peers": [],
			"node_token": "minted-node-token-xyz"
		}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	cfg.CoordClient = coordapi.New(mock.URL)
	cfg.NodeTokenPath = filepath.Join(t.TempDir(), "node.token")

	// Simulate a fresh, never-paired machine whose tunnel bring-up already
	// generated a wg.key (as ensureKeypair/tunnel.New always do) but that has
	// NO node.token on disk - the true "never enrolled" state.
	keyPath := cfg.WGPrivKeyPath
	if err := os.MkdirAll(filepath.Dir(keyPath), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(keyPath, []byte("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="), 0600); err != nil {
		t.Fatal(err)
	}

	client, _ := testServer(t, cfg)

	body := strings.NewReader(`{"pairing_token":"GOODTOKEN1234567890123456789012"}`)
	resp, err := client.Post("http://localhost/pair", "application/json", body)
	if err != nil {
		t.Fatalf("POST /pair: %v", err)
	}
	defer resp.Body.Close()

	// Must reach the coordinator and succeed - not a local 409 short-circuit.
	if resp.StatusCode != http.StatusCreated {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 201 (pairing should proceed to the coordinator), got %d: %s", resp.StatusCode, b)
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
	// Same /tmp socket-path scheme as testServer - t.TempDir produces paths
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
// POST /pair - Ed25519 signing-key registration (DERP-token PoP)
// ---------------------------------------------------------------------------

// TestHandlePair_Success_RegistersSigningKey exercises the full happy path:
// a successful /pair must (1) attach signing_public_key to the coordinator's
// EnrollRequest, using the same key this node loads/generates locally, and
// (2) mark that exact key as registered locally only after the coordinator
// actually accepted it - this is the ordering guard from
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
// half of the ordering guard: if the coordinator rejects the enroll call, the
// signing key must not be marked registered - signing before confirmed
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
// POST /unpair - signing-key + marker cleanup
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

// ---------------------------------------------------------------------------
// POST /pair / POST /unpair - node_token persistence (per-node bearer
// credential; matches the coordinator's EnrollResponse.node_token)
// ---------------------------------------------------------------------------

// TestHandlePair_Success_PersistsNodeToken exercises the primary
// behaviour: a successful /pair must (1) persist the coordinator's freshly
// minted node_token to disk at NodeTokenPath, mode 0600, and (2) install it
// as the live credential on the shared coordapi.Client so the very next
// coordinator call (e.g. the roster-reconcile loop's ListPeers) presents it.
func TestHandlePair_Success_PersistsNodeToken(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mesh/enroll" {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, `{
			"node_id": "11111111-1111-1111-1111-111111111111",
			"household_id": "22222222-2222-2222-2222-222222222222",
			"mesh_ip": "100.64.0.7",
			"mesh_cidr": "100.64.0.0/24",
			"peers": [],
			"node_token": "minted-node-token-xyz"
		}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	coordClient := coordapi.New(mock.URL)
	cfg.CoordClient = coordClient
	nodeTokenPath := filepath.Join(t.TempDir(), "node.token")
	cfg.NodeTokenPath = nodeTokenPath

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

	// Persisted to disk verbatim.
	raw, err := os.ReadFile(nodeTokenPath)
	if err != nil {
		t.Fatalf("read node token file: %v", err)
	}
	if string(raw) != "minted-node-token-xyz" {
		t.Errorf("persisted node token: got %q want %q", string(raw), "minted-node-token-xyz")
	}

	// Live value installed on the shared coordapi.Client.
	if got := coordClient.NodeToken(); got != "minted-node-token-xyz" {
		t.Errorf("coordClient.NodeToken(): got %q want %q", got, "minted-node-token-xyz")
	}
}

// TestHandlePair_CoordinatorError_DoesNotPersistNodeToken is the negative
// half: a rejected enroll must leave no node_token file behind and must not
// touch the shared client's live value.
func TestHandlePair_CoordinatorError_DoesNotPersistNodeToken(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"pairing token not found"}`)
	}))
	defer mock.Close()

	cfg := defaultCfg(t)
	coordClient := coordapi.New(mock.URL)
	cfg.CoordClient = coordClient
	nodeTokenPath := filepath.Join(t.TempDir(), "node.token")
	cfg.NodeTokenPath = nodeTokenPath

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
	if _, err := os.Stat(nodeTokenPath); !os.IsNotExist(err) {
		t.Errorf("expected no node token file after a rejected enroll, stat err=%v", err)
	}
	if got := coordClient.NodeToken(); got != "" {
		t.Errorf("coordClient.NodeToken() after rejected enroll: got %q want empty", got)
	}
}

// TestHandleUnpair_ClearsNodeToken mirrors the signing-key cleanup test: a
// stale node_token must not survive /unpair, on disk or in the shared
// client's live value - a fresh /pair afterwards mints and persists a
// completely new one.
func TestHandleUnpair_ClearsNodeToken(t *testing.T) {
	cfg := defaultCfg(t)
	coordClient := coordapi.New("http://unused.invalid")
	cfg.CoordClient = coordClient
	nodeTokenPath := filepath.Join(t.TempDir(), "node.token")
	cfg.NodeTokenPath = nodeTokenPath

	if err := os.WriteFile(nodeTokenPath, []byte("stale-token"), 0600); err != nil {
		t.Fatalf("seed node token file: %v", err)
	}
	coordClient.SetNodeToken("stale-token")

	client, _ := testServer(t, cfg)

	resp, err := client.Post("http://localhost/unpair", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /unpair: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", resp.StatusCode)
	}

	if _, err := os.Stat(nodeTokenPath); !os.IsNotExist(err) {
		t.Errorf("expected node token file to be removed, stat err=%v", err)
	}
	if got := coordClient.NodeToken(); got != "" {
		t.Errorf("coordClient.NodeToken() after unpair: got %q want empty", got)
	}
}

// TestHandleUnpair_NilCoordClient_DoesNotPanic pins that /unpair works even
// when no CoordClient was wired (mirrors TestHandleUnpair's defaultCfg,
// which never sets one) - the node-token live-clear step must be a guarded
// no-op, not a nil-pointer panic, for any caller that hasn't wired
// node-token support at all.
func TestHandleUnpair_NilCoordClient_DoesNotPanic(t *testing.T) {
	cfg := defaultCfg(t) // cfg.CoordClient is nil
	client, _ := testServer(t, cfg)

	resp, err := client.Post("http://localhost/unpair", "application/json", nil)
	if err != nil {
		t.Fatalf("POST /unpair: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Errorf("expected 204, got %d", resp.StatusCode)
	}
}

// ---------------------------------------------------------------------------
// PATCH /settings
// ---------------------------------------------------------------------------

func TestHandleSettingsPatch_OmittedField_EchoesCurrentValue(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.State.SetCoordinatorURL("http://127.0.0.1:8400")
	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `{}`)
	defer resp.Body.Close()
	checkProtocolHeader(t, resp)
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, b)
	}

	var body struct {
		CoordinatorURL string `json:"coordinator_url"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.CoordinatorURL != "http://127.0.0.1:8400" {
		t.Errorf("coordinator_url = %q, want unchanged %q", body.CoordinatorURL, "http://127.0.0.1:8400")
	}
}

func TestHandleSettingsPatch_ValidURL_UpdatesStateAndPersists(t *testing.T) {
	cfg := defaultCfg(t)
	cfg.State.SetCoordinatorURL("http://127.0.0.1:8400")
	settingsPath := filepath.Join(t.TempDir(), "settings.json")
	cfg.SettingsPath = settingsPath

	var applied []string
	cfg.ApplyCoordinatorURLFn = func(url string) {
		applied = append(applied, url)
		cfg.State.SetCoordinatorURL(url) // mimic main.go's applyCoordinatorURL side effect
	}

	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `{"coordinator_url":"https://coordinator.example.com"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, b)
	}

	var body struct {
		CoordinatorURL string `json:"coordinator_url"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.CoordinatorURL != "https://coordinator.example.com" {
		t.Errorf("response coordinator_url = %q, want %q", body.CoordinatorURL, "https://coordinator.example.com")
	}

	// ApplyCoordinatorURLFn was invoked exactly once with the normalized URL.
	if len(applied) != 1 || applied[0] != "https://coordinator.example.com" {
		t.Errorf("ApplyCoordinatorURLFn calls = %v, want exactly one call with %q", applied, "https://coordinator.example.com")
	}

	// State reflects the live update.
	if got := cfg.State.CoordinatorURL(); got != "https://coordinator.example.com" {
		t.Errorf("State.CoordinatorURL() = %q, want %q", got, "https://coordinator.example.com")
	}

	// Persisted to settings.json.
	saved, err := runtimesettings.Load(settingsPath)
	if err != nil {
		t.Fatalf("Load(settingsPath): %v", err)
	}
	if saved.CoordinatorURL != "https://coordinator.example.com" {
		t.Errorf("persisted coordinator_url = %q, want %q", saved.CoordinatorURL, "https://coordinator.example.com")
	}

	// Persisted file is mode 0600 (secrets-adjacent convention).
	info, err := os.Stat(settingsPath)
	if err != nil {
		t.Fatalf("stat settings file: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("settings file mode = %o, want 0600", perm)
	}
}

func TestHandleSettingsPatch_TrailingSlash_Normalized(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `{"coordinator_url":"https://coordinator.example.com/"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, b)
	}

	var body struct {
		CoordinatorURL string `json:"coordinator_url"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.CoordinatorURL != "https://coordinator.example.com" {
		t.Errorf("coordinator_url = %q, want trailing slash trimmed to %q", body.CoordinatorURL, "https://coordinator.example.com")
	}
}

func TestHandleSettingsPatch_InvalidURL_Rejected(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"empty string", `{"coordinator_url":""}`},
		{"missing scheme", `{"coordinator_url":"coordinator.example.com"}`},
		{"unsupported scheme", `{"coordinator_url":"ftp://coordinator.example.com"}`},
		{"scheme only, no host", `{"coordinator_url":"https://"}`},
		{"not a string", `{"coordinator_url":123}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := defaultCfg(t)
			cfg.State.SetCoordinatorURL("http://127.0.0.1:8400")
			settingsPath := filepath.Join(t.TempDir(), "settings.json")
			cfg.SettingsPath = settingsPath
			applyCalled := false
			cfg.ApplyCoordinatorURLFn = func(string) { applyCalled = true }

			client, _ := testServer(t, cfg)
			resp := patch(t, client, "/settings", tc.body)
			defer resp.Body.Close()
			io.Copy(io.Discard, resp.Body) //nolint:errcheck

			if resp.StatusCode != http.StatusUnprocessableEntity && resp.StatusCode != http.StatusBadRequest {
				t.Errorf("expected 422 or 400, got %d", resp.StatusCode)
			}
			if applyCalled {
				t.Error("ApplyCoordinatorURLFn must not be called for a rejected update")
			}
			if got := cfg.State.CoordinatorURL(); got != "http://127.0.0.1:8400" {
				t.Errorf("State.CoordinatorURL() = %q, want unchanged %q", got, "http://127.0.0.1:8400")
			}
			if _, err := os.Stat(settingsPath); !os.IsNotExist(err) {
				t.Errorf("expected no settings file for a rejected update, stat err=%v", err)
			}
		})
	}
}

func TestHandleSettingsPatch_MalformedJSON_400(t *testing.T) {
	cfg := defaultCfg(t)
	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `not json`)
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body) //nolint:errcheck

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("expected 400, got %d", resp.StatusCode)
	}
}

// TestHandleSettingsPatch_NilApplyFn_FallsBackToStateOnly pins the
// documented fallback: when ApplyCoordinatorURLFn isn't wired (tests, or a
// caller that only wants the persisted/State value), the handler still
// succeeds and updates State directly rather than panicking on a nil call.
func TestHandleSettingsPatch_NilApplyFn_FallsBackToStateOnly(t *testing.T) {
	cfg := defaultCfg(t) // cfg.ApplyCoordinatorURLFn is nil
	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `{"coordinator_url":"https://coordinator.example.com"}`)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected 200, got %d: %s", resp.StatusCode, b)
	}
	if got := cfg.State.CoordinatorURL(); got != "https://coordinator.example.com" {
		t.Errorf("State.CoordinatorURL() = %q, want %q", got, "https://coordinator.example.com")
	}
}

// TestHandleSettingsPatch_NoSettingsPath_StillAppliesLive pins that
// SettingsPath == "" (disables persistence - the documented test-only
// escape hatch) still applies the live update; it just won't survive a
// restart.
func TestHandleSettingsPatch_NoSettingsPath_StillAppliesLive(t *testing.T) {
	cfg := defaultCfg(t) // cfg.SettingsPath is ""
	var applied string
	cfg.ApplyCoordinatorURLFn = func(url string) { applied = url }
	client, _ := testServer(t, cfg)

	resp := patch(t, client, "/settings", `{"coordinator_url":"https://coordinator.example.com"}`)
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body) //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", resp.StatusCode)
	}
	if applied != "https://coordinator.example.com" {
		t.Errorf("ApplyCoordinatorURLFn called with %q, want %q", applied, "https://coordinator.example.com")
	}
}

// ---------------------------------------------------------------------------
// GET (startup) - persisted override reload-on-startup lives in main.go, not
// this package; internal/runtimesettings has its own Load/Save round-trip
// tests. This package's tests stop at "handleSettingsPatch persists +
// applies correctly" - see internal/runtimesettings/runtimesettings_test.go
// for the storage layer and cmd/potluck-mesh for the startup-load wiring.
// ---------------------------------------------------------------------------
