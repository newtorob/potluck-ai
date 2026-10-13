package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/signingkey"
)

// ---------------------------------------------------------------------------
// buildDERPTokenRequestBody - the ordering guard
// ---------------------------------------------------------------------------

// TestDERPTokenBody_UnregisteredKeyOmitsSignature is the ordering-guard test:
// a node with a local signing key that has not been confirmed registered
// with the coordinator (no MarkRegistered call ever happened for it - e.g.
// an already-enrolled pre-upgrade node, or one mid-pairing) must send a
// request with neither nonce nor signature. Sending either without the other
// (or a signature the coordinator can't verify) is a 400/401 per
// routers/mesh.py::issue_derp_token.
func TestDERPTokenBody_UnregisteredKeyOmitsSignature(t *testing.T) {
	dir := t.TempDir()
	keyPath := filepath.Join(dir, "signing.key")

	// Generating the key (as main.go does unconditionally at startup) but
	// Never calling signingkey.MarkRegistered - this is the exact state of
	// an upgraded-but-not-re-enrolled node.
	if _, err := signingkey.LoadOrGenerate(keyPath); err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	body := buildDERPTokenRequestBody("wgpubkey==", "ord", keyPath)

	if _, ok := body["nonce"]; ok {
		t.Errorf("nonce must be absent for an unregistered signing key, got %q", body["nonce"])
	}
	if _, ok := body["signature"]; ok {
		t.Errorf("signature must be absent for an unregistered signing key, got %q", body["signature"])
	}
	if body["wg_public_key"] != "wgpubkey==" {
		t.Errorf("wg_public_key: got %q", body["wg_public_key"])
	}
	if body["region"] != "ord" {
		t.Errorf("region: got %q", body["region"])
	}
}

// TestDERPTokenBody_NoKeyFileYetOmitsSignature covers the case where the
// signing key file doesn't even exist yet at the configured path (e.g. an
// unexpected filesystem issue) - LoadOrGenerate transparently creates one,
// but it's fresh and therefore unregistered, so the outcome must be
// identical to the already-generated-but-unregistered case above.
func TestDERPTokenBody_NoKeyFileYetOmitsSignature(t *testing.T) {
	dir := t.TempDir()
	keyPath := filepath.Join(dir, "signing.key") // never created before this call

	body := buildDERPTokenRequestBody("wgpubkey==", "ord", keyPath)

	if _, ok := body["nonce"]; ok {
		t.Error("nonce must be absent when the signing key was only just generated")
	}
	if _, ok := body["signature"]; ok {
		t.Error("signature must be absent when the signing key was only just generated")
	}
}

// TestDERPTokenBody_RegisteredKeySignsRequest is the happy path: once
// MarkRegistered has recorded this exact public key as accepted by the
// coordinator, the request must carry a fresh unix-seconds nonce plus a
// signature that verifies against the keypair's own public key over exactly
// derp-token:{wg_public_key}:{region}:{nonce}.
func TestDERPTokenBody_RegisteredKeySignsRequest(t *testing.T) {
	dir := t.TempDir()
	keyPath := filepath.Join(dir, "signing.key")

	kp, err := signingkey.LoadOrGenerate(keyPath)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if err := signingkey.MarkRegistered(keyPath, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}

	before := time.Now().Unix()
	body := buildDERPTokenRequestBody("wgpubkey==", "ord", keyPath)
	after := time.Now().Unix()

	nonceStr, ok := body["nonce"]
	if !ok {
		t.Fatal("expected nonce to be present for a registered signing key")
	}
	sigStr, ok := body["signature"]
	if !ok {
		t.Fatal("expected signature to be present for a registered signing key")
	}

	nonce, err := strconv.ParseInt(nonceStr, 10, 64)
	if err != nil {
		t.Fatalf("nonce is not a unix timestamp: %v", err)
	}
	if nonce < before || nonce > after {
		t.Errorf("nonce %d not within [%d,%d]", nonce, before, after)
	}

	// The signature must verify over the exact coordinator message format.
	msg := "derp-token:wgpubkey==:ord:" + nonceStr
	pubBytes, err := base64.RawURLEncoding.DecodeString(kp.PublicKeyB64)
	if err != nil {
		t.Fatalf("decode pubkey: %v", err)
	}
	sigBytes, err := base64.RawURLEncoding.DecodeString(sigStr)
	if err != nil {
		t.Fatalf("decode signature: %v", err)
	}
	if !ed25519.Verify(pubBytes, []byte(msg), sigBytes) {
		t.Error("signature does not verify over derp-token:{wg_public_key}:{region}:{nonce}")
	}
}

// TestDERPTokenBody_MismatchedRegisteredKeyOmitsSignature: if the marker
// records a different public key than the one currently on disk (e.g. the
// key file was replaced without a fresh enroll), the request must fall back
// to unsigned - never sign with a key the coordinator doesn't actually have
// on file, which would 401.
func TestDERPTokenBody_MismatchedRegisteredKeyOmitsSignature(t *testing.T) {
	dir := t.TempDir()
	keyPath := filepath.Join(dir, "signing.key")

	if _, err := signingkey.LoadOrGenerate(keyPath); err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	// Register a different (bogus) pubkey than the one actually on disk.
	if err := signingkey.MarkRegistered(keyPath, "some-other-node-entirely"); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}

	body := buildDERPTokenRequestBody("wgpubkey==", "ord", keyPath)
	if _, ok := body["nonce"]; ok {
		t.Error("nonce must be absent when the on-disk key doesn't match the registration marker")
	}
	if _, ok := body["signature"]; ok {
		t.Error("signature must be absent when the on-disk key doesn't match the registration marker")
	}
}
