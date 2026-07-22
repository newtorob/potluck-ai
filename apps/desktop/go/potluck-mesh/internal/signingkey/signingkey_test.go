package signingkey

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// ---------------------------------------------------------------------------
// Generation + persistence + reuse
// ---------------------------------------------------------------------------

func TestLoadOrGenerate_GeneratesAndPersists(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")

	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("precondition: key file should not exist yet")
	}

	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if kp.PublicKeyB64 == "" {
		t.Fatal("PublicKeyB64 is empty")
	}
	// 32 raw bytes, base64url no padding -> 43 chars, no '=' padding.
	if len(kp.PublicKeyB64) != 43 {
		t.Errorf("PublicKeyB64 length: got %d want 43 (32 raw bytes, unpadded b64url)", len(kp.PublicKeyB64))
	}
	if strContains(kp.PublicKeyB64, "=") {
		t.Errorf("PublicKeyB64 must not contain padding: %q", kp.PublicKeyB64)
	}

	if _, err := os.Stat(path); err != nil {
		t.Fatalf("key file was not persisted: %v", err)
	}
}

func TestLoadOrGenerate_FilePermissions0600(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX file mode bits aren't meaningful on Windows")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")

	if _, err := LoadOrGenerate(path); err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("private key file mode: got %o want 0600", perm)
	}
}

func TestLoadOrGenerate_ReusesExistingKey(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")

	first, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("first LoadOrGenerate: %v", err)
	}

	second, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("second LoadOrGenerate: %v", err)
	}

	if first.PublicKeyB64 != second.PublicKeyB64 {
		t.Errorf("key not reused: first=%s second=%s", first.PublicKeyB64, second.PublicKeyB64)
	}

	// Signatures from both loads must verify against the same public key,
	// proving the private key material (not just the derived pubkey string)
	// round-trips identically too.
	msg := []byte("reuse-check")
	sig1 := first.Sign(msg)
	sig2 := second.Sign(msg)
	pubBytes, err := base64.RawURLEncoding.DecodeString(first.PublicKeyB64)
	if err != nil {
		t.Fatalf("decode pubkey: %v", err)
	}
	sigBytes1, _ := base64.RawURLEncoding.DecodeString(sig1)
	sigBytes2, _ := base64.RawURLEncoding.DecodeString(sig2)
	if !ed25519.Verify(pubBytes, msg, sigBytes1) {
		t.Error("signature from first load does not verify")
	}
	if !ed25519.Verify(pubBytes, msg, sigBytes2) {
		t.Error("signature from second load does not verify")
	}
}

func TestLoadOrGenerate_CreatesParentDir(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "sub", "signing.key")

	if _, err := LoadOrGenerate(path); err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("key file not created in nested dir: %v", err)
	}
}

func TestLoadOrGenerate_RejectsWrongLengthKey(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	if err := os.WriteFile(path, []byte(base64.StdEncoding.EncodeToString([]byte("too-short"))), 0600); err != nil {
		t.Fatal(err)
	}

	if _, err := LoadOrGenerate(path); err == nil {
		t.Fatal("expected error loading a wrong-length key, got nil")
	}
}

// ---------------------------------------------------------------------------
// Signed-message format + round-trip verification
// ---------------------------------------------------------------------------

// TestDERPTokenMessage_ExactFormat pins the message format to
// derp-token:{pubkey}:{region}:{nonce}, matching
// routers/mesh.py::_verify_derp_pop's
// f"derp-token:{req.wg_public_key}:{req.region}:{req.nonce}" byte-for-byte.
func TestDERPTokenMessage_ExactFormat(t *testing.T) {
	got := string(DERPTokenMessage("PUBKEY==", "ord", "1234567890"))
	want := "derp-token:PUBKEY==:ord:1234567890"
	if got != want {
		t.Errorf("DERPTokenMessage: got %q want %q", got, want)
	}
}

// TestSignVerify_RoundTrip proves Sign() produces a base64url (no padding)
// signature that verifies against the KeyPair's own public key over the
// exact derp-token message format.
func TestSignVerify_RoundTrip(t *testing.T) {
	dir := t.TempDir()
	kp, err := LoadOrGenerate(filepath.Join(dir, "signing.key"))
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	msg := DERPTokenMessage("wgpubkeyABC=", "ord", "1700000000")
	sigB64 := kp.Sign(msg)

	if strContains(sigB64, "=") {
		t.Errorf("signature must not contain padding: %q", sigB64)
	}

	pubBytes, err := base64.RawURLEncoding.DecodeString(kp.PublicKeyB64)
	if err != nil {
		t.Fatalf("decode pubkey: %v", err)
	}
	sigBytes, err := base64.RawURLEncoding.DecodeString(sigB64)
	if err != nil {
		t.Fatalf("decode signature: %v", err)
	}
	if !ed25519.Verify(pubBytes, msg, sigBytes) {
		t.Error("signature does not verify against the keypair's own public key")
	}

	// Tampering with any one field must invalidate the signature.
	tampered := DERPTokenMessage("wgpubkeyABC=", "ord", "1700000001")
	if ed25519.Verify(pubBytes, tampered, sigBytes) {
		t.Error("signature verified against a tampered message — should not")
	}
}

// TestCrossCheck_MirrorsCoordinatorDecode reproduces
// coordinator/crypto.py's b64url_decode + verify_ed25519_signature decode
// convention byte-for-byte in Go (pad the unpadded base64url string to a
// multiple of 4 with '=', then standard base64.URLEncoding decode) and
// proves it produces IDENTICAL bytes to Go's own RawURLEncoding decode, then
// runs the same ed25519 verification the coordinator would. This is the
// end-to-end proof that the Go client's wire encoding matches what
// coordinator/routers/mesh.py::_verify_derp_pop actually decodes.
func TestCrossCheck_MirrorsCoordinatorDecode(t *testing.T) {
	dir := t.TempDir()
	kp, err := LoadOrGenerate(filepath.Join(dir, "signing.key"))
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	wgPubKey := "somewgpubkeyBASE64standard=="
	region := "ord"
	nonce := "1753142400"
	msg := DERPTokenMessage(wgPubKey, region, nonce)
	sigB64 := kp.Sign(msg)

	// --- mirror coordinator/crypto.py::b64url_decode for the public key --- //
	pubBytes, err := pythonB64URLDecode(kp.PublicKeyB64)
	if err != nil {
		t.Fatalf("pythonB64URLDecode(pubkey): %v", err)
	}
	if len(pubBytes) != ed25519.PublicKeySize {
		t.Fatalf("decoded pubkey wrong length: got %d want %d", len(pubBytes), ed25519.PublicKeySize)
	}
	// Must be byte-identical to Go's own RawURLEncoding decode of the same
	// string — proves the two decode conventions agree exactly.
	rawDecoded, err := base64.RawURLEncoding.DecodeString(kp.PublicKeyB64)
	if err != nil {
		t.Fatalf("RawURLEncoding decode: %v", err)
	}
	if string(pubBytes) != string(rawDecoded) {
		t.Fatal("python-style b64url_decode and Go RawURLEncoding disagree on the public key bytes")
	}

	// --- mirror coordinator/crypto.py::b64url_decode for the signature --- //
	sigBytes, err := pythonB64URLDecode(sigB64)
	if err != nil {
		t.Fatalf("pythonB64URLDecode(signature): %v", err)
	}
	if len(sigBytes) != ed25519.SignatureSize {
		t.Fatalf("decoded signature wrong length: got %d want %d", len(sigBytes), ed25519.SignatureSize)
	}

	// --- mirror routers/mesh.py::_verify_derp_pop's message reconstruction --- //
	coordinatorMsg := "derp-token:" + wgPubKey + ":" + region + ":" + nonce

	if !ed25519.Verify(pubBytes, []byte(coordinatorMsg), sigBytes) {
		t.Fatal("signature does not verify under the coordinator's own decode convention — wire encoding mismatch")
	}
}

// pythonB64URLDecode reproduces coordinator/crypto.py::b64url_decode
// exactly:
//
//	def b64url_decode(s: str) -> bytes:
//	    pad = "=" * (-len(s) % 4)
//	    return base64.urlsafe_b64decode(s + pad)
func pythonB64URLDecode(s string) ([]byte, error) {
	padLen := (4 - len(s)%4) % 4
	padded := s + repeat("=", padLen)
	return base64.URLEncoding.DecodeString(padded)
}

func repeat(s string, n int) string {
	out := make([]byte, 0, n)
	for i := 0; i < n; i++ {
		out = append(out, s[0])
	}
	return string(out)
}

func strContains(s, substr string) bool {
	for i := 0; i+len(substr) <= len(s); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Registration marker (the ordering guard)
// ---------------------------------------------------------------------------

func TestRegistration_AbsentByDefault(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	if IsRegistered(path, kp.PublicKeyB64) {
		t.Error("a freshly generated key must not be considered registered until MarkRegistered is called")
	}
}

func TestRegistration_MarkThenIsRegistered(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}

	if err := MarkRegistered(path, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}
	if !IsRegistered(path, kp.PublicKeyB64) {
		t.Error("expected IsRegistered=true after MarkRegistered with the same pubkey")
	}
}

func TestRegistration_MismatchedKeyIsNotRegistered(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if err := MarkRegistered(path, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}

	// Simulate the key file being regenerated without a fresh enroll (e.g.
	// manual deletion of just the key, not the marker): a DIFFERENT pubkey
	// must never be treated as registered just because *some* marker exists.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	newKP, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate (regenerate): %v", err)
	}
	if newKP.PublicKeyB64 == kp.PublicKeyB64 {
		t.Fatal("test invariant broken: regenerated key must differ from the original")
	}
	if IsRegistered(path, newKP.PublicKeyB64) {
		t.Error("a regenerated key must not inherit the old key's registration marker")
	}
}

func TestRegistration_ClearRegistered(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if err := MarkRegistered(path, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}
	if !IsRegistered(path, kp.PublicKeyB64) {
		t.Fatal("precondition: expected registered")
	}

	if err := ClearRegistered(path); err != nil {
		t.Fatalf("ClearRegistered: %v", err)
	}
	if IsRegistered(path, kp.PublicKeyB64) {
		t.Error("expected IsRegistered=false after ClearRegistered")
	}

	// Idempotent: clearing again (marker already gone) must not error.
	if err := ClearRegistered(path); err != nil {
		t.Errorf("ClearRegistered on an already-cleared marker returned an error: %v", err)
	}
}

func TestRegistration_MarkerFilePermissions0600(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX file mode bits aren't meaningful on Windows")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "signing.key")
	kp, err := LoadOrGenerate(path)
	if err != nil {
		t.Fatalf("LoadOrGenerate: %v", err)
	}
	if err := MarkRegistered(path, kp.PublicKeyB64); err != nil {
		t.Fatalf("MarkRegistered: %v", err)
	}

	info, err := os.Stat(RegisteredMarkerPath(path))
	if err != nil {
		t.Fatalf("stat marker: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("marker file mode: got %o want 0600", perm)
	}
}
