package localapi

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/circlelink"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// fixturePath: from internal/<pkg> the repository root is six directories up.
const fixturePath = "../../../../../../services/coordinator/tests/fixtures/circle_link_shapes.json"

func loadSidecarFixture(t *testing.T) (status, circlePeers json.RawMessage) {
	t.Helper()
	b, err := os.ReadFile(fixturePath)
	if err != nil {
		abs, _ := filepath.Abs(fixturePath)
		t.Fatalf("cannot read the shared circle-link fixture at %s: %v", abs, err)
	}
	var f struct {
		Sidecar map[string]json.RawMessage `json:"sidecar"`
	}
	if err := json.Unmarshal(b, &f); err != nil {
		t.Fatal(err)
	}
	return f.Sidecar["status_addition"], f.Sidecar["circle_peers"]
}

func testKeyOf(seed byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = seed
	}
	return base64.StdEncoding.EncodeToString(b)
}

type fakeCircle struct {
	views   []circlelink.PeerView
	cleared int
}

func (f *fakeCircle) Peers() []circlelink.PeerView { return f.views }
func (f *fakeCircle) Clear()                       { f.cleared++ }

func newInternalServer(t *testing.T, cfg Config) *Server {
	t.Helper()
	if cfg.Bus == nil {
		cfg.Bus = events.NewBus()
	}
	if cfg.PeerStore == nil {
		cfg.PeerStore = peerstate.New()
	}
	if cfg.State == nil {
		cfg.State = NewState()
	}
	cfg.WGPrivKeyPath = filepath.Join(t.TempDir(), "wg.key")
	cfg.SocketPath = filepath.Join(t.TempDir(), "s")
	return New(cfg)
}

func serve(t *testing.T, h http.HandlerFunc, method, path string) []byte {
	t.Helper()
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(method, path, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("%s %s = %d: %s", method, path, rec.Code, rec.Body.String())
	}
	return rec.Body.Bytes()
}

// shapeDiff compares a response with a fixture example: the same keys at every
// level, the same JSON types, the same strings and integers. Floats that depend
// on the clock (handshake_age_seconds) need only be numbers near the example.
func shapeDiff(path string, got, want any) []string {
	var out []string
	switch w := want.(type) {
	case map[string]any:
		g, ok := got.(map[string]any)
		if !ok {
			return []string{fmt.Sprintf("%s: got %T, fixture has an object", path, got)}
		}
		for k := range w {
			if _, ok := g[k]; !ok {
				out = append(out, fmt.Sprintf("%s.%s: missing from the response", path, k))
			}
		}
		for k := range g {
			if _, ok := w[k]; !ok {
				out = append(out, fmt.Sprintf("%s.%s: in the response but not in the fixture", path, k))
			}
		}
		for k, wv := range w {
			if gv, ok := g[k]; ok {
				out = append(out, shapeDiff(path+"."+k, gv, wv)...)
			}
		}
	case []any:
		g, ok := got.([]any)
		if !ok || len(g) != len(w) {
			return []string{fmt.Sprintf("%s: got %v, fixture has a list of %d", path, got, len(w))}
		}
		for i := range w {
			out = append(out, shapeDiff(fmt.Sprintf("%s[%d]", path, i), g[i], w[i])...)
		}
	case float64:
		g, ok := got.(float64)
		switch {
		case !ok:
			out = append(out, fmt.Sprintf("%s: got %T, fixture has a number", path, got))
		case strings.HasSuffix(path, "handshake_age_seconds"):
			if math.Abs(g-w) > 1 {
				out = append(out, fmt.Sprintf("%s: %v is not near %v", path, g, w))
			}
		case g != w:
			out = append(out, fmt.Sprintf("%s: %v, fixture has %v", path, g, w))
		}
	default:
		if !reflect.DeepEqual(got, want) {
			out = append(out, fmt.Sprintf("%s: %v (%T), fixture has %v (%T)", path, got, got, want, want))
		}
	}
	return out
}

func decodeStrict(t *testing.T, name string, raw []byte, v any) {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		t.Errorf("%s does not decode with unknown fields disallowed: %v", name, err)
	}
}

// The fixture's /circle-peers example decodes into the response types with
// unknown fields disallowed, and the live handler, given the same data, writes
// the same keys, types and values.
func TestCirclePeersMatchesTheFixture(t *testing.T) {
	_, fixture := loadSidecarFixture(t)
	var typed circlePeersResponse
	decodeStrict(t, "sidecar.circle_peers", fixture, &typed)
	if len(typed.Peers) != 2 {
		t.Fatalf("fixture has %d peers", len(typed.Peers))
	}

	keyDirect, keyConnecting := testKeyOf(0x31), testKeyOf(0x32)
	src := &fakeCircle{views: []circlelink.PeerView{
		{MeshIP: "100.64.0.7", PubKey: keyDirect, Transport: "direct",
			Inbound: &circlelink.GrantView{LinkID: "c0ffee00-1111-4222-8333-444455556666", Port: 8347, TokenSHA256: "45827150f595bab19278b82d22eba203d126486c59732655abb65ea67d209ee6", ExpiresInS: 412}},
		{MeshIP: "100.64.1.2", PubKey: keyConnecting, Transport: "derp",
			Outbound: &circlelink.GrantView{LinkID: "d00dfeed-1111-4222-8333-444455556666", Port: 8340, ExpiresInS: 590}},
	}}
	srv := newInternalServer(t, Config{Circle: src, PeerReachFn: func(k string) (time.Time, uint64, uint64, error) {
		if k == keyDirect {
			return time.Now().Add(-3200 * time.Millisecond), 0, 0, nil
		}
		return time.Time{}, 0, 0, nil
	}})
	body := serve(t, srv.handleCirclePeers, "GET", "/circle-peers")

	var got, want any
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(fixture, &want); err != nil {
		t.Fatal(err)
	}
	if diffs := shapeDiff("$", got, want); len(diffs) > 0 {
		t.Errorf("/circle-peers differs from the fixture:\n  %s\nbody: %s", strings.Join(diffs, "\n  "), body)
	}
	// The response types decode the live body strictly too.
	var live circlePeersResponse
	decodeStrict(t, "live /circle-peers", body, &live)
}

// Deliberately absent: anything that identifies the other machine beyond its
// mesh address, and anything a UI could show as a stranger's address.
func TestCirclePeersLeaksNothingAboutTheOtherMachine(t *testing.T) {
	key := testKeyOf(0x41)
	src := &fakeCircle{views: []circlelink.PeerView{{MeshIP: "100.64.1.2", PubKey: key, Transport: "direct",
		Inbound: &circlelink.GrantView{LinkID: "c0ffee00-1111-4222-8333-444455556666", Port: 8347, TokenSHA256: strings.Repeat("ab", 32), ExpiresInS: 5}}}}
	srv := newInternalServer(t, Config{Circle: src})
	body := string(serve(t, srv.handleCirclePeers, "GET", "/circle-peers"))
	for _, banned := range []string{key, key[:10], "endpoint", "node_name", "node_id", "wg_public_key", "hostname", "last_seen", "platform", "member"} {
		if strings.Contains(body, banned) {
			t.Errorf("/circle-peers contains %q: %s", banned, body)
		}
	}
	var got struct {
		Peers []map[string]any `json:"peers"`
	}
	_ = json.Unmarshal([]byte(body), &got)
	allowed := map[string]bool{"scope": true, "mesh_ip": true, "reachable": true, "reachability_mode": true, "handshake_age_seconds": true, "inbound": true, "outbound": true}
	for k := range got.Peers[0] {
		if !allowed[k] {
			t.Errorf("unexpected key %q", k)
		}
	}
}

// The rules for reachable and reachability_mode: a handshake under three
// minutes old, "connecting" while there is a transport and no handshake, null
// when there is nothing to connect with.
func TestCirclePeersReachabilityRules(t *testing.T) {
	key := testKeyOf(0x51)
	cases := []struct {
		name      string
		transport string
		age       time.Duration // 0: never
		reachable bool
		mode      any
	}{
		{"fresh direct", "direct", 5 * time.Second, true, "direct"},
		{"fresh relayed", "derp", 5 * time.Second, true, "derp"},
		{"two minutes fifty-nine", "derp", 179 * time.Second, true, "derp"},
		{"three minutes one", "derp", 181 * time.Second, false, "connecting"},
		{"never, relay", "derp", 0, false, "connecting"},
		{"never, direct try", "direct", 0, false, "connecting"},
		{"never, nothing to connect with", "none", 0, false, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			src := &fakeCircle{views: []circlelink.PeerView{{MeshIP: "100.64.1.2", PubKey: key, Transport: tc.transport,
				Outbound: &circlelink.GrantView{LinkID: "c0ffee00-1111-4222-8333-444455556666", Port: 8347, ExpiresInS: 60}}}}
			srv := newInternalServer(t, Config{Circle: src, PeerReachFn: func(string) (time.Time, uint64, uint64, error) {
				if tc.age == 0 {
					return time.Time{}, 0, 0, nil
				}
				return time.Now().Add(-tc.age), 0, 0, nil
			}})
			var got struct {
				Peers []struct {
					Reachable bool     `json:"reachable"`
					Mode      any      `json:"reachability_mode"`
					Age       *float64 `json:"handshake_age_seconds"`
				} `json:"peers"`
			}
			if err := json.Unmarshal(serve(t, srv.handleCirclePeers, "GET", "/circle-peers"), &got); err != nil {
				t.Fatal(err)
			}
			p := got.Peers[0]
			if p.Reachable != tc.reachable || p.Mode != tc.mode {
				t.Errorf("reachable=%v mode=%v, want %v %v", p.Reachable, p.Mode, tc.reachable, tc.mode)
			}
			if (tc.age == 0) != (p.Age == nil) {
				t.Errorf("handshake_age_seconds = %v for age %v", p.Age, tc.age)
			}
		})
	}
}

// A circle peer is in /circle-peers and nowhere else: not in /peers, not in
// peers_online_count, not in the connecting count.
func TestCirclePeerIsNeverListedAsAHouseholdPeer(t *testing.T) {
	ps := peerstate.New()
	if _, rej := ps.Reset("100.64.0.6", []signal.RosterPeer{{NodeID: "n-b", NodeName: "node-b", MeshIP: "100.64.0.7", WGPublicKey: testKeyOf(0x61)}}); len(rej) != 0 {
		t.Fatal(rej)
	}
	src := &fakeCircle{views: []circlelink.PeerView{
		{MeshIP: "100.64.1.2", PubKey: testKeyOf(0x62), Transport: "derp", Inbound: &circlelink.GrantView{LinkID: "c0ffee00-1111-4222-8333-444455556666", Port: 8347, TokenSHA256: strings.Repeat("ab", 32), ExpiresInS: 5}},
		{MeshIP: "100.64.2.2", PubKey: testKeyOf(0x63), Transport: "derp", Outbound: &circlelink.GrantView{LinkID: "d00dfeed-1111-4222-8333-444455556666", Port: 8347, ExpiresInS: 5}},
	}}
	srv := newInternalServer(t, Config{PeerStore: ps, Circle: src,
		IsDERPRoutedFn: func(string) bool { return true },
		PeerReachFn:    func(string) (time.Time, uint64, uint64, error) { return time.Time{}, 0, 0, nil }})

	peers := string(serve(t, srv.handlePeers, "GET", "/peers"))
	for _, banned := range []string{"100.64.1.2", "100.64.2.2", `"circle"`, testKeyOf(0x62), testKeyOf(0x63)} {
		if strings.Contains(peers, banned) {
			t.Errorf("/peers lists a circle peer (%s): %s", banned, peers)
		}
	}
	var pp struct {
		Peers []struct {
			Scope string `json:"scope"`
		} `json:"peers"`
	}
	_ = json.Unmarshal([]byte(peers), &pp)
	if len(pp.Peers) != 1 || pp.Peers[0].Scope != "household" {
		t.Errorf("/peers = %s", peers)
	}

	var st struct {
		Online     int `json:"peers_online_count"`
		Connecting int `json:"peers_connecting_count"`
	}
	if err := json.Unmarshal(serve(t, srv.handleStatus, "GET", "/status"), &st); err != nil {
		t.Fatal(err)
	}
	if st.Online != 1 || st.Connecting != 1 {
		t.Errorf("peers_online_count=%d peers_connecting_count=%d, want 1 and 1 (household only)", st.Online, st.Connecting)
	}

	var cp struct {
		Peers []struct {
			Scope string `json:"scope"`
		} `json:"peers"`
	}
	_ = json.Unmarshal(serve(t, srv.handleCirclePeers, "GET", "/circle-peers"), &cp)
	if len(cp.Peers) != 2 || cp.Peers[0].Scope != "circle" {
		t.Errorf("/circle-peers = %+v", cp)
	}
}

func TestStatusFeaturesMatchTheFixture(t *testing.T) {
	status, _ := loadSidecarFixture(t)
	var want struct {
		Features []string `json:"features"`
	}
	decodeStrict(t, "sidecar.status_addition", status, &want)

	st := NewState()
	st.SetFeatures(signal.SidecarFeatures())
	srv := newInternalServer(t, Config{State: st})
	var got struct {
		Features []string `json:"features"`
	}
	if err := json.Unmarshal(serve(t, srv.handleStatus, "GET", "/status"), &got); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got.Features, want.Features) {
		t.Errorf("features = %v, fixture %v", got.Features, want.Features)
	}
	// A sidecar with nothing to advertise says so with an empty list.
	srv = newInternalServer(t, Config{})
	var bare map[string]any
	if err := json.Unmarshal(serve(t, srv.handleStatus, "GET", "/status"), &bare); err != nil {
		t.Fatal(err)
	}
	if f, ok := bare["features"].([]any); !ok || len(f) != 0 {
		t.Errorf("status without features: features = %#v, want an empty list", bare["features"])
	}
}

func TestCirclePeersEmptyWithoutAController(t *testing.T) {
	srv := newInternalServer(t, Config{})
	var got map[string]any
	if err := json.Unmarshal(serve(t, srv.handleCirclePeers, "GET", "/circle-peers"), &got); err != nil {
		t.Fatal(err)
	}
	if p, ok := got["peers"].([]any); !ok || len(p) != 0 || len(got) != 1 {
		t.Errorf("body = %#v, want {\"peers\": []}", got)
	}
}

func TestUnpairClearsCirclePeers(t *testing.T) {
	src := &fakeCircle{}
	srv := newInternalServer(t, Config{Circle: src})
	rec := httptest.NewRecorder()
	srv.handleUnpair(rec, httptest.NewRequest("POST", "/unpair", nil))
	if rec.Code != http.StatusNoContent {
		t.Fatalf("unpair = %d: %s", rec.Code, rec.Body.String())
	}
	if src.cleared != 1 {
		t.Errorf("Clear called %d times", src.cleared)
	}
}
