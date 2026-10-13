// Package signingkey manages the per-node Ed25519 keypair used only for the
// DERP-token proof-of-possession (PoP) challenge (POST /mesh/derp-token
// nonce+signature). This is deliberately a separate keypair from the node's
// WireGuard identity: WG keys are Curve25519 (Diffie-Hellman only, per RFC
// 7748) and are not usable for signing. See:
//
//   - services/coordinator/coordinator/crypto.py - decode/verify helpers
//   - services/coordinator/coordinator/schemas/mesh.py - DERPTokenRequest,
//     EnrollRequest.signing_public_key wire shapes
//   - services/coordinator/coordinator/routers/mesh.py - _verify_derp_pop,
//     the exact signed-message format
//
// Wire encoding (must match coordinator/crypto.py exactly):
//   - Public key: base64url, no padding, of the raw 32-byte Ed25519 public
//     key (base64.RawURLEncoding in Go == Python's b64url_decode convention
//     of stripping/adding "=" padding around standard urlsafe_b64decode).
//   - Signature: base64url, no padding, of the raw 64-byte Ed25519 signature.
//   - Signed message: exactly `derp-token:{wg_public_key}:{region}:{nonce}`
//     (UTF-8), where wg_public_key/region/nonce are the exact strings sent on
//     the wire in the DERPTokenRequest body - see DERPTokenMessage.
//
// Registration ordering (the rollout footgun this package exists to avoid):
// the coordinator 401s a DERP-token request that carries nonce+signature if
// the calling node has no signing_public_key registered (see
// routers/mesh.py::_verify_derp_pop). A node's signing_public_key is only
// registered when POST /mesh/enroll succeeds with it attached - enroll is a
// one-time redemption of a pairing token, not something that re-runs on every
// process start. So an already-enrolled node upgrading to a binary that links
// this package must not start signing merely because it now has a local
// keypair; it must only sign once it has itself completed a fresh enroll
// carrying that exact key. MarkRegistered/IsRegistered below are the local
// record of that fact - see internal/localapi/server.go's handlePair (writes
// the marker on a successful enroll) and cmd/potluck-mesh's DERP-token
// fetcher (checks the marker before signing).
package signingkey

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
)

// KeyPair holds a node's Ed25519 signing keypair.
type KeyPair struct {
	priv ed25519.PrivateKey

	// PublicKeyB64 is the base64url (no padding) encoding of the raw 32-byte
	// Ed25519 public key. This is exactly the string to send as
	// EnrollRequest.signing_public_key.
	PublicKeyB64 string
}

// Sign signs msg with the keypair's private key and returns the base64url
// (no padding) encoded signature - exactly the string to send as
// DERPTokenRequest.signature.
func (k *KeyPair) Sign(msg []byte) string {
	sig := ed25519.Sign(k.priv, msg)
	return base64.RawURLEncoding.EncodeToString(sig)
}

// DERPTokenMessage builds the exact message string the coordinator expects
// to be signed for a DERP-token PoP request. Must match
// routers/mesh.py::_verify_derp_pop's
// `f"derp-token:{req.wg_public_key}:{req.region}:{req.nonce}"` byte-for-byte
// (same separator, same field order, same UTF-8 encoding via plain string
// concatenation - no whitespace, no trailing newline).
func DERPTokenMessage(wgPublicKey, region, nonce string) []byte {
	return []byte(fmt.Sprintf("derp-token:%s:%s:%s", wgPublicKey, region, nonce))
}

// LoadOrGenerate reads the Ed25519 private key (32-byte seed, standard
// base64) from path. If the file does not exist, a fresh keypair is
// generated and the seed is written to path at mode 0600 (parent dirs
// created at 0700) - mirrors util.WGGenerateAndSavePrivateKey's convention
// for the WireGuard key. Generate-once-reuse-thereafter: subsequent calls
// with the same path return the same keypair. Never logs the private key.
func LoadOrGenerate(path string) (*KeyPair, error) {
	raw, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("signingkey: read private key %s: %w", path, err)
	}
	if os.IsNotExist(err) {
		return generateAndSave(path)
	}

	seed, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		return nil, fmt.Errorf("signingkey: decode private key %s: %w", path, err)
	}
	if len(seed) != ed25519.SeedSize {
		return nil, fmt.Errorf("signingkey: private key %s wrong length: want %d bytes, got %d",
			path, ed25519.SeedSize, len(seed))
	}
	priv := ed25519.NewKeyFromSeed(seed)
	pub, _ := priv.Public().(ed25519.PublicKey)
	return &KeyPair{
		priv:         priv,
		PublicKeyB64: base64.RawURLEncoding.EncodeToString(pub),
	}, nil
}

func generateAndSave(path string) (*KeyPair, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("signingkey: generate keypair: %w", err)
	}

	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, fmt.Errorf("signingkey: create key directory %s: %w", dir, err)
	}

	encoded := base64.StdEncoding.EncodeToString(priv.Seed())
	if err := os.WriteFile(path, []byte(encoded), 0600); err != nil {
		return nil, fmt.Errorf("signingkey: write private key to %s: %w", path, err)
	}

	return &KeyPair{
		priv:         priv,
		PublicKeyB64: base64.RawURLEncoding.EncodeToString(pub),
	}, nil
}

// ---------------------------------------------------------------------------
// Registration tracking (the ordering guard)
// ---------------------------------------------------------------------------

// registeredMarkerSuffix names the sidecar file (next to the private key)
// that records the signing_public_key value most recently accepted by the
// coordinator via a successful POST /mesh/enroll.
const registeredMarkerSuffix = ".registered"

// RegisteredMarkerPath returns the marker file path for a given signing
// private-key path.
func RegisteredMarkerPath(privKeyPath string) string {
	return privKeyPath + registeredMarkerSuffix
}

// MarkRegistered records that the coordinator just accepted pubKeyB64 as
// this node's signing_public_key (a successful POST /mesh/enroll that
// included it). Call this only after that HTTP call has actually succeeded -
// never speculatively. Mode 0600, same as the private key itself (contents
// aren't secret, but there's no reason to leave it group/world readable).
func MarkRegistered(privKeyPath, pubKeyB64 string) error {
	if err := os.WriteFile(RegisteredMarkerPath(privKeyPath), []byte(pubKeyB64), 0600); err != nil {
		return fmt.Errorf("signingkey: write registered marker: %w", err)
	}
	return nil
}

// IsRegistered reports whether the coordinator is known (from this node's own
// local record) to hold signing_public_key = pubKeyB64. This is the only
// signal callers should trust before attaching nonce+signature to a
// DERP-token request - see the package doc for why. False (marker absent,
// unreadable, or recording a different public key - e.g. the key file was
// regenerated without a fresh enroll) means the caller must omit
// nonce+signature and fall back to the legacy wg_public_key-possession-only
// request shape, which the coordinator always accepts while
// POTLUCK_DERP_POP_REQUIRED is off (the current, and only currently safe,
// prod state).
func IsRegistered(privKeyPath, pubKeyB64 string) bool {
	raw, err := os.ReadFile(RegisteredMarkerPath(privKeyPath))
	if err != nil {
		return false
	}
	return string(raw) == pubKeyB64
}

// ClearRegistered removes the registration marker. Called from /unpair
// alongside wiping the signing private key itself, so a subsequent /pair
// starts from a clean, unregistered state and only starts signing again
// once a fresh enroll actually confirms registration.
func ClearRegistered(privKeyPath string) error {
	err := os.Remove(RegisteredMarkerPath(privKeyPath))
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("signingkey: remove registered marker: %w", err)
	}
	return nil
}
