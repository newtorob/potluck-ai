package coordapi_test

import (
	"encoding/json"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
)

// TestEnrollRequest_SigningPublicKey_OmittedWhenNil pins the back-compat
// shape: a node that couldn't produce a signing key (or predates PoP
// support) must send NO signing_public_key key at all — not null — matching
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
// value used when a signing key IS present — must be exactly
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
