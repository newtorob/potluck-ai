package coordapi_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
)

// TestEnrollRequest_SigningPublicKey_OmittedWhenNil pins the back-compat
// shape: a node that couldn't produce a signing key (or predates PoP
// support) must send NO signing_public_key key at all - not null - matching
// coordinator/schemas/mesh.py::EnrollRequest.signing_public_key being
// Optional (Pydantic accepts a missing key the same as an explicit null, but
// omitting it entirely is the more conservative wire shape and what
// `omitempty` on a nil *string produces).
func TestEnrollRequest_SigningPublicKey_OmittedWhenNil(t *testing.T) {
	req := coordapi.EnrollRequest{
		PairingToken: "TOKEN",
		WGPublicKey:  "PUBKEY",
		NodeName:     "node-1",
	}
	b, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if _, ok := raw["signing_public_key"]; ok {
		t.Errorf("signing_public_key must be omitted when nil, got present in %s", b)
	}
}

// TestEnrollRequest_SigningPublicKey_IncludedWhenSet pins the field name and
// value used when a signing key IS present - must be exactly
// "signing_public_key" (coordinator/schemas/mesh.py::EnrollRequest field
// name) carrying the base64url-no-padding string produced by
// internal/signingkey.KeyPair.PublicKeyB64.
func TestEnrollRequest_SigningPublicKey_IncludedWhenSet(t *testing.T) {
	pub := "abcDEF123_-xyzABCdef123_-xyzABCdef123AB" // 43-char b64url-shaped stand-in
	req := coordapi.EnrollRequest{
		PairingToken:     "TOKEN",
		WGPublicKey:      "PUBKEY",
		NodeName:         "node-1",
		SigningPublicKey: &pub,
	}
	b, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	got, ok := raw["signing_public_key"]
	if !ok {
		t.Fatalf("signing_public_key missing from %s", b)
	}
	var gotStr string
	if err := json.Unmarshal(got, &gotStr); err != nil {
		t.Fatalf("unmarshal signing_public_key value: %v", err)
	}
	if gotStr != pub {
		t.Errorf("signing_public_key value: got %q want %q", gotStr, pub)
	}
}

// ---------------------------------------------------------------------------
// BaseURL / SetBaseURL - runtime coordinator-URL override support
// ---------------------------------------------------------------------------

func TestClient_BaseURL_ReturnsConstructorValue(t *testing.T) {
	c := coordapi.New("http://127.0.0.1:8400")
	if got := c.BaseURL(); got != "http://127.0.0.1:8400" {
		t.Errorf("BaseURL() = %q, want %q", got, "http://127.0.0.1:8400")
	}
}

func TestClient_SetBaseURL_UpdatesLiveValue(t *testing.T) {
	c := coordapi.New("http://127.0.0.1:8400")
	c.SetBaseURL("https://coordinator.example.com")
	if got := c.BaseURL(); got != "https://coordinator.example.com" {
		t.Errorf("BaseURL() after SetBaseURL = %q, want %q", got, "https://coordinator.example.com")
	}
}

// TestClient_SetBaseURL_ConcurrentWithListPeers is the concurrency-safety
// test for the runtime coordinator-URL
// override: a live SetBaseURL call can race an in-flight ListPeers call from
// the roster-reconcile loop (cmd/potluck-mesh/main.go's
// runRosterRefreshLoop). Run under -race; a bare string field here would
// fail this test with a data race.
func TestClient_SetBaseURL_ConcurrentWithListPeers(t *testing.T) {
	mock := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `[]`)
	}))
	defer mock.Close()

	c := coordapi.New(mock.URL)

	var wg sync.WaitGroup
	stop := make(chan struct{})

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 50; i++ {
			c.SetBaseURL(mock.URL)
		}
		close(stop)
	}()

	wg.Add(1)
	go func() {
		defer wg.Done()
		ctx := context.Background()
		for {
			select {
			case <-stop:
				return
			default:
				_, _ = c.ListPeers(ctx, "pubkey")
			}
		}
	}()

	wg.Wait()
}
