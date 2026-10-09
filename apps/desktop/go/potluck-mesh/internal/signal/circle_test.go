package signal_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// ---------------------------------------------------------------- the shared fixture

// fixturePath is the contract between the coordinator, this sidecar and the
// engine. From internal/<pkg> the repository root is six directories up.
const fixturePath = "../../../../../../services/coordinator/tests/fixtures/circle_link_shapes.json"

type fixtureFile struct {
	Enums   map[string][]any           `json:"enums"`
	Vectors map[string]string          `json:"vectors"`
	Signal  map[string]json.RawMessage `json:"signal"`
}

func loadFixture(t *testing.T) fixtureFile {
	t.Helper()
	abs, _ := filepath.Abs(fixturePath)
	b, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatalf("cannot read the shared circle-link fixture at %s: %v", abs, err)
	}
	var f fixtureFile
	if err := json.Unmarshal(b, &f); err != nil {
		t.Fatal(err)
	}
	if len(f.Signal) < 10 {
		t.Fatalf("the fixture has only %d signal examples; the loader is looking at the wrong file", len(f.Signal))
	}
	return f
}

// strict decodes raw into v and fails on any field v does not have.
func strict(t *testing.T, name string, raw json.RawMessage, v any) {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		t.Errorf("%s: does not decode into %T with unknown fields disallowed: %v", name, v, err)
	}
}

// sameJSON compares two JSON documents as values, so key order is irrelevant but
// a missing key or a null that became absent is a difference.
func sameJSON(t *testing.T, name string, got any, want json.RawMessage) {
	t.Helper()
	gb, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var g, w any
	if err := json.Unmarshal(gb, &g); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(want, &w); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(g, w) {
		t.Errorf("%s: re-encoded form differs from the fixture\n got: %s\nwant: %s", name, gb, want)
	}
}

// Every signal example in the fixture decodes into this package's types with
// unknown fields disallowed, and encodes back to the same document. An example
// this test does not know fails it, so a new message cannot be added to the
// fixture and forgotten here.
func TestFixtureSignalExamplesDecodeStrictlyAndRoundTrip(t *testing.T) {
	f := loadFixture(t)
	names := make([]string, 0, len(f.Signal))
	for n := range f.Signal {
		names = append(names, n)
	}
	sort.Strings(names)
	seen := map[string]int{}
	for _, name := range names {
		raw := f.Signal[name]
		switch {
		case name == "hello":
			var v signal.Hello
			strict(t, name, raw, &v)
			sameJSON(t, name, v, raw)
			if len(v.Features) != 1 || v.Features[0] != signal.FeatureCircleLinkV1 {
				t.Errorf("hello features = %v", v.Features)
			}
		case strings.HasPrefix(name, "circle_peer_add"):
			var v signal.CirclePeerAdd
			strict(t, name, raw, &v)
			sameJSON(t, name, v, raw)
		case strings.HasPrefix(name, "circle_peer_extend"):
			var v signal.CirclePeerExtend
			strict(t, name, raw, &v)
			sameJSON(t, name, v, raw)
		case strings.HasPrefix(name, "circle_peer_remove"):
			var v signal.CirclePeerRemove
			strict(t, name, raw, &v)
			sameJSON(t, name, v, raw)
		case strings.HasPrefix(name, "circle_peer_ack"):
			var v signal.CirclePeerAck
			strict(t, name, raw, &v)
			sameJSON(t, name, v, raw)
		default:
			t.Errorf("the fixture has a signal example %q that this sidecar does not decode", name)
			continue
		}
		seen[strings.SplitN(name, "_ack", 2)[0]]++
	}
	for _, want := range []string{"circle_peer_add_lender", "circle_peer_add_requester", "circle_peer_add_requester_lan",
		"circle_peer_add_lender_relay_only", "circle_peer_add_requester_relay_only", "circle_peer_extend",
		"circle_peer_extend_relay_only", "circle_peer_remove", "circle_peer_ack_add_ready", "circle_peer_ack_add_rejected",
		"circle_peer_ack_extend_ready", "circle_peer_ack_extend_rejected", "hello"} {
		if _, ok := f.Signal[want]; !ok {
			t.Errorf("the fixture no longer has %q; the test list is stale", want)
		}
	}
}

// The strings this package uses for roles, ops, states and reasons are the
// fixture's closed sets, no more and no fewer.
func TestFixtureEnumsMatchTheConstants(t *testing.T) {
	f := loadFixture(t)
	str := func(name string) []string {
		var out []string
		for _, v := range f.Enums[name] {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
		sort.Strings(out)
		return out
	}
	eq := func(name string, got ...string) {
		sort.Strings(got)
		if want := str(name); !reflect.DeepEqual(got, want) {
			t.Errorf("enum %s: constants %v, fixture %v", name, got, want)
		}
	}
	eq("role", signal.CircleRoleLender, signal.CircleRoleRequester)
	eq("remove_reason", signal.CircleRemoveEnded, signal.CircleRemoveRevoked, signal.CircleRemoveExpired, signal.CircleRemoveSuperseded)
	eq("ack_op", signal.CircleOpAdd, signal.CircleOpExtend)
	eq("ack_state", signal.CircleStateReady, signal.CircleStateRejected)
	eq("ack_reason", signal.CircleReasonInvalidPeer, signal.CircleReasonAddressConflict, signal.CircleReasonLimit,
		signal.CircleReasonUnsupported, signal.CircleReasonNotEnrolled, signal.CircleReasonTunnelError, signal.CircleReasonUnknownGrant)
	eq("feature", signal.SidecarFeatures()...)
}

// The acks this sidecar builds equal the fixture's examples.
func TestBuiltAcksEqualTheFixtureExamples(t *testing.T) {
	f := loadFixture(t)
	const link = "c0ffee00-1111-4222-8333-444455556666"
	sameJSON(t, "ack add ready", signal.NewCirclePeerAck(link, signal.CircleOpAdd, signal.CircleStateReady, ""), f.Signal["circle_peer_ack_add_ready"])
	sameJSON(t, "ack add rejected", signal.NewCirclePeerAck(link, signal.CircleOpAdd, signal.CircleStateRejected, signal.CircleReasonAddressConflict), f.Signal["circle_peer_ack_add_rejected"])
	sameJSON(t, "ack extend ready", signal.NewCirclePeerAck(link, signal.CircleOpExtend, signal.CircleStateReady, ""), f.Signal["circle_peer_ack_extend_ready"])
	sameJSON(t, "ack extend rejected", signal.NewCirclePeerAck(link, signal.CircleOpExtend, signal.CircleStateRejected, signal.CircleReasonUnknownGrant), f.Signal["circle_peer_ack_extend_rejected"])
	// A reason on a ready ack is dropped.
	if a := signal.NewCirclePeerAck(link, signal.CircleOpAdd, signal.CircleStateReady, "limit"); a.Reason != nil {
		t.Error("a ready ack carries a reason")
	}
}

func TestValidLinkID(t *testing.T) {
	for _, ok := range []string{"c0ffee00-1111-4222-8333-444455556666", "00000000-0000-0000-0000-000000000000"} {
		if !signal.ValidLinkID(ok) {
			t.Errorf("%q should be valid", ok)
		}
	}
	for _, bad := range []string{"", "x", "C0FFEE00-1111-4222-8333-444455556666", "c0ffee00-1111-4222-8333-44445555666", "c0ffee00-1111-4222-8333-4444555566667",
		"c0ffee0001111422283334444555566666", "c0ffee00_1111_4222_8333_444455556666", "c0ffee00-1111-4222-8333-44445555666\n", "g0ffee00-1111-4222-8333-444455556666"} {
		if signal.ValidLinkID(bad) {
			t.Errorf("%q should be invalid", bad)
		}
	}
}

// ---------------------------------------------------------------- the client

type circleRecorder struct {
	mu    sync.Mutex
	calls []string
	adds  []signal.CirclePeerAdd
}

func (r *circleRecorder) OnCirclePeerAdd(m signal.CirclePeerAdd) {
	r.mu.Lock()
	r.calls = append(r.calls, "add "+m.LinkID)
	r.adds = append(r.adds, m)
	r.mu.Unlock()
}
func (r *circleRecorder) OnCirclePeerExtend(m signal.CirclePeerExtend) {
	r.mu.Lock()
	r.calls = append(r.calls, "extend "+m.LinkID)
	r.mu.Unlock()
}
func (r *circleRecorder) OnCirclePeerRemove(m signal.CirclePeerRemove) {
	r.mu.Lock()
	r.calls = append(r.calls, "remove "+m.LinkID)
	r.mu.Unlock()
}
func (r *circleRecorder) list() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.calls...)
}

// scriptServer completes hello/welcome, records the hello, sends the scripted
// raw messages, and records everything the client sends back.
type scriptServer struct {
	srv   *httptest.Server
	mu    sync.Mutex
	hello json.RawMessage
	got   []json.RawMessage
}

func newScriptServer(t *testing.T, script ...string) *scriptServer {
	s := &scriptServer{}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer c.CloseNow() //nolint:errcheck
		ctx := r.Context()
		var hello json.RawMessage
		if err := wsjson.Read(ctx, c, &hello); err != nil {
			return
		}
		s.mu.Lock()
		s.hello = hello
		s.mu.Unlock()
		_ = wsjson.Write(ctx, c, signal.Welcome{Type: "welcome", NodeID: "n", HouseholdID: "h", MeshIP: "100.64.0.6"})
		for _, m := range script {
			if err := c.Write(ctx, websocket.MessageText, []byte(m)); err != nil {
				return
			}
		}
		for {
			var raw json.RawMessage
			if err := wsjson.Read(ctx, c, &raw); err != nil {
				return
			}
			s.mu.Lock()
			s.got = append(s.got, raw)
			s.mu.Unlock()
		}
	}))
	t.Cleanup(s.srv.Close)
	return s
}

func (s *scriptServer) url() string { return "ws" + s.srv.URL[len("http"):] + "/mesh/signal" }
func (s *scriptServer) received() []json.RawMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]json.RawMessage(nil), s.got...)
}
func (s *scriptServer) helloMsg() json.RawMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.hello
}

func runClient(t *testing.T, c *signal.Client) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = c.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for: %s", what)
}

func TestHelloAdvertisesTheFeaturesItWasGiven(t *testing.T) {
	s := newScriptServer(t)
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	c.SetFeatures(signal.SidecarFeatures())
	runClient(t, c)
	eventually(t, "hello", func() bool { return s.helloMsg() != nil })
	var h signal.Hello
	strict(t, "hello", s.helloMsg(), &h)
	if len(h.Features) != 1 || h.Features[0] != "circle_link_v1" || h.Type != "hello" || h.WGPublicKey != "pk" {
		t.Errorf("hello = %+v", h)
	}
}

// A client that was given no features sends the hello it always did, with no
// features key at all.
func TestHelloWithoutFeaturesHasNoFeaturesKey(t *testing.T) {
	s := newScriptServer(t)
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	runClient(t, c)
	eventually(t, "hello", func() bool { return s.helloMsg() != nil })
	if strings.Contains(string(s.helloMsg()), "features") {
		t.Errorf("hello = %s", s.helloMsg())
	}
}

func TestCircleMessagesReachTheHandlerInOrder(t *testing.T) {
	f := loadFixture(t)
	s := newScriptServer(t,
		string(f.Signal["circle_peer_remove"]),
		string(f.Signal["circle_peer_add_lender"]),
		string(f.Signal["circle_peer_extend"]),
		`{"type":"circle_peer_add","link_id":"d00dfeed-1111-4222-8333-444455556666","role":"requester","peer_wg_public_key":"AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=","peer_mesh_ip":"100.64.1.2","port":8347,"ttl_s":5,"token_sha256":null,"peer_endpoint":null,"peer_local_endpoints":[],"direct_window_ms":0,"a_field_from_the_future":1}`,
	)
	rec := &circleRecorder{}
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	c.SetCircleHandler(rec)
	runClient(t, c)
	eventually(t, "four messages", func() bool { return len(rec.list()) == 4 })
	want := []string{
		"remove c0ffee00-1111-4222-8333-444455556666",
		"add c0ffee00-1111-4222-8333-444455556666",
		"extend c0ffee00-1111-4222-8333-444455556666",
		"add d00dfeed-1111-4222-8333-444455556666",
	}
	if got := rec.list(); !reflect.DeepEqual(got, want) {
		t.Errorf("handler calls = %v, want %v", got, want)
	}
	// Unknown fields are ignored on the live path, as the coordinator may grow them.
	if rec.adds[1].Port != 8347 || rec.adds[1].Role != "requester" {
		t.Errorf("fields = %+v", rec.adds[1])
	}
	if rec.adds[0].TokenSHA256 == nil || *rec.adds[0].TokenSHA256 == "" {
		t.Error("the lender add lost its token hash")
	}
}

// An add whose fields do not decode (a port that is a string) is answered with a
// rejected ack when its link_id can be read; one with no usable link_id is not
// answered at all.
func TestUndecodableAddIsRejectedWhenItsLinkIDIsReadable(t *testing.T) {
	s := newScriptServer(t,
		`{"type":"circle_peer_add","link_id":"c0ffee00-1111-4222-8333-444455556666","role":"lender","port":"eighty"}`,
		`{"type":"circle_peer_add","link_id":"not-a-uuid\n","port":"eighty"}`,
		`{"type":"circle_peer_extend","link_id":"d00dfeed-1111-4222-8333-444455556666","ttl_s":"soon"}`,
	)
	rec := &circleRecorder{}
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	c.SetCircleHandler(rec)
	runClient(t, c)
	eventually(t, "two acks", func() bool { return len(s.received()) >= 2 })
	time.Sleep(100 * time.Millisecond)
	got := s.received()
	if len(got) != 2 {
		t.Fatalf("client sent %d messages, want 2 acks: %s", len(got), got)
	}
	var a1, a2 signal.CirclePeerAck
	strict(t, "ack1", got[0], &a1)
	strict(t, "ack2", got[1], &a2)
	if a1.Op != "add" || a1.State != "rejected" || *a1.Reason != "invalid_peer" || a1.LinkID != "c0ffee00-1111-4222-8333-444455556666" {
		t.Errorf("ack 1 = %+v", a1)
	}
	if a2.Op != "extend" || a2.State != "rejected" || a2.LinkID != "d00dfeed-1111-4222-8333-444455556666" {
		t.Errorf("ack 2 = %+v", a2)
	}
	if len(rec.list()) != 0 {
		t.Errorf("an undecodable message reached the handler: %v", rec.list())
	}
}

// With no circle handler the messages are ignored, as an old sidecar would.
func TestCircleMessagesAreIgnoredWithoutAHandler(t *testing.T) {
	f := loadFixture(t)
	s := newScriptServer(t, string(f.Signal["circle_peer_add_lender"]), string(f.Signal["circle_peer_remove"]))
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	runClient(t, c)
	time.Sleep(300 * time.Millisecond)
	if n := len(s.received()); n != 0 {
		t.Errorf("a sidecar with no handler answered %d times", n)
	}
}

func TestSendCirclePeerAckReachesTheServer(t *testing.T) {
	s := newScriptServer(t)
	c := signal.NewClient(s.url(), "pk", "", nil, noopHandler{})
	runClient(t, c)
	eventually(t, "connected", func() bool { return s.helloMsg() != nil })
	time.Sleep(100 * time.Millisecond)
	c.SendCirclePeerAck(signal.NewCirclePeerAck("c0ffee00-1111-4222-8333-444455556666", "add", "ready", ""))
	eventually(t, "the ack", func() bool { return len(s.received()) == 1 })
	if !strings.Contains(string(s.received()[0]), `"reason":null`) {
		t.Errorf("ready ack = %s, want an explicit null reason", s.received()[0])
	}
	// With no connection the call returns and sends nothing.
	idle := signal.NewClient("ws://127.0.0.1:1/mesh/signal", "pk", "", nil, noopHandler{})
	idle.SendCirclePeerAck(signal.NewCirclePeerAck("c0ffee00-1111-4222-8333-444455556666", "add", "ready", ""))
}
