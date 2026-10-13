package coordapi_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
)

// capturingHandler records the last request's headers and responds with the
// given status/body for every call.
func capturingHandler(t *testing.T, status int, body string) (http.HandlerFunc, func() http.Header) {
	t.Helper()
	var captured http.Header
	h := func(w http.ResponseWriter, r *http.Request) {
		captured = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}
	return h, func() http.Header { return captured }
}

// TestEnroll_AttachesBearerAndLegacyHeaders pins the wire contract: POST
// /mesh/enroll carries BOTH Authorization: Bearer <node_token> (whenever one
// is loaded) AND X-Potluck-Coordinator-Token (whenever configured) -
// matching coordinator/routers/mesh.py::enroll, which accepts a valid
// pairing_token in the body OR the coordinator token OR (for a re-pair after
// node_token support shipped, not the normal case) a bearer, so sending
// whatever's available is always safe.
func TestEnroll_AttachesBearerAndLegacyHeaders(t *testing.T) {
	h, headers := capturingHandler(t, http.StatusCreated, `{"node_id":"n1","household_id":"h1","mesh_ip":"100.64.0.6","mesh_cidr":"100.64.0.0/24","peers":[],"node_token":"minted-tok"}`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL)
	c.CoordinatorToken = "legacy-secret"
	c.SetNodeToken("preexisting-node-tok")

	_, err := c.Enroll(context.Background(), coordapi.EnrollRequest{
		PairingToken: "PTOKEN",
		WGPublicKey:  "PUBKEY",
		NodeName:     "node-1",
	})
	if err != nil {
		t.Fatalf("Enroll: %v", err)
	}

	got := headers()
	if v := got.Get("Authorization"); v != "Bearer preexisting-node-tok" {
		t.Errorf("Authorization header: got %q want %q", v, "Bearer preexisting-node-tok")
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "legacy-secret" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q want %q", v, "legacy-secret")
	}
}

// TestEnroll_NoTokensAtAll_SendsNeitherHeader pins the primary distributed-app
// path: a fresh install enrolls with nothing but the pairing token in the
// body - no coordinator token configured, no node_token minted yet - and
// this client must not send either auth header (the coordinator's enroll()
// treats a valid pairing_token in the body as sufficient on its own).
func TestEnroll_NoTokensAtAll_SendsNeitherHeader(t *testing.T) {
	h, headers := capturingHandler(t, http.StatusCreated, `{"node_id":"n1","household_id":"h1","mesh_ip":"100.64.0.6","mesh_cidr":"100.64.0.0/24","peers":[],"node_token":"minted-tok"}`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL) // CoordinatorToken unset, node token never set

	_, err := c.Enroll(context.Background(), coordapi.EnrollRequest{
		PairingToken: "PTOKEN",
		WGPublicKey:  "PUBKEY",
		NodeName:     "node-1",
	})
	if err != nil {
		t.Fatalf("Enroll: %v", err)
	}

	got := headers()
	if v := got.Get("Authorization"); v != "" {
		t.Errorf("Authorization header: got %q, want absent", v)
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q, want absent", v)
	}
}

// TestEnroll_ParsesNodeToken pins that EnrollResponse.NodeToken round-trips
// through this client's JSON decoding untouched - the caller (localapi's
// handlePair) persists this value verbatim.
func TestEnroll_ParsesNodeToken(t *testing.T) {
	h, _ := capturingHandler(t, http.StatusCreated, `{"node_id":"n1","household_id":"h1","mesh_ip":"100.64.0.6","mesh_cidr":"100.64.0.0/24","peers":[],"node_token":"minted-tok-xyz"}`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL)
	resp, err := c.Enroll(context.Background(), coordapi.EnrollRequest{
		PairingToken: "PTOKEN",
		WGPublicKey:  "PUBKEY",
		NodeName:     "node-1",
	})
	if err != nil {
		t.Fatalf("Enroll: %v", err)
	}
	if resp.NodeToken != "minted-tok-xyz" {
		t.Errorf("NodeToken: got %q want %q", resp.NodeToken, "minted-tok-xyz")
	}
}

// TestListPeers_AttachesBearerAndLegacyHeaders mirrors the Enroll case for
// GET /mesh/peers - gated by resolve_node_from_token server-side.
func TestListPeers_AttachesBearerAndLegacyHeaders(t *testing.T) {
	h, headers := capturingHandler(t, http.StatusOK, `[]`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL)
	c.CoordinatorToken = "legacy-secret"
	c.SetNodeToken("node-tok-abc")

	_, err := c.ListPeers(context.Background(), "PUBKEY")
	if err != nil {
		t.Fatalf("ListPeers: %v", err)
	}

	got := headers()
	if v := got.Get("Authorization"); v != "Bearer node-tok-abc" {
		t.Errorf("Authorization header: got %q want %q", v, "Bearer node-tok-abc")
	}
	if v := got.Get("X-Potluck-Coordinator-Token"); v != "legacy-secret" {
		t.Errorf("X-Potluck-Coordinator-Token header: got %q want %q", v, "legacy-secret")
	}
}

// TestListPeers_NoNodeToken_OmitsBearer pins the not-yet-enrolled /
// pre-node_token-support case: no garbage "Bearer " header, just whatever
// legacy credential (if any) is configured.
func TestListPeers_NoNodeToken_OmitsBearer(t *testing.T) {
	h, headers := capturingHandler(t, http.StatusOK, `[]`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL)

	_, err := c.ListPeers(context.Background(), "PUBKEY")
	if err != nil {
		t.Fatalf("ListPeers: %v", err)
	}

	got := headers()
	if v := got.Get("Authorization"); v != "" {
		t.Errorf("Authorization header: got %q, want absent", v)
	}
}

// TestSetNodeToken_UpdatesLiveValue pins that SetNodeToken takes effect on
// the VERY NEXT call - the mechanism internal/localapi's handlePair relies
// on to make a freshly-minted token immediately usable.
func TestSetNodeToken_UpdatesLiveValue(t *testing.T) {
	h, headers := capturingHandler(t, http.StatusOK, `[]`)
	srv := httptest.NewServer(h)
	defer srv.Close()

	c := coordapi.New(srv.URL)
	if got := c.NodeToken(); got != "" {
		t.Fatalf("NodeToken before SetNodeToken: got %q want empty", got)
	}

	if _, err := c.ListPeers(context.Background(), "PUBKEY"); err != nil {
		t.Fatalf("ListPeers (pre-token): %v", err)
	}
	if v := headers().Get("Authorization"); v != "" {
		t.Errorf("Authorization header before SetNodeToken: got %q, want absent", v)
	}

	c.SetNodeToken("freshly-minted")
	if got := c.NodeToken(); got != "freshly-minted" {
		t.Errorf("NodeToken after SetNodeToken: got %q want %q", got, "freshly-minted")
	}

	if _, err := c.ListPeers(context.Background(), "PUBKEY"); err != nil {
		t.Fatalf("ListPeers (post-token): %v", err)
	}
	if v := headers().Get("Authorization"); v != "Bearer freshly-minted" {
		t.Errorf("Authorization header after SetNodeToken: got %q want %q", v, "Bearer freshly-minted")
	}

	// Clearing (the /unpair path) must remove it again.
	c.SetNodeToken("")
	if _, err := c.ListPeers(context.Background(), "PUBKEY"); err != nil {
		t.Fatalf("ListPeers (post-clear): %v", err)
	}
	if v := headers().Get("Authorization"); v != "" {
		t.Errorf("Authorization header after clearing: got %q, want absent", v)
	}
}
