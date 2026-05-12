package localapi

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/crypto/curve25519"
)

const wgKeyLen = 32

// ensureKeypair returns the base64-encoded WG public key for this node.
// If a private key already exists at privKeyPath it is loaded; otherwise a
// fresh Curve25519 keypair is generated, the private key is written to disk
// at mode 0600, and the public key is returned.
//
// The private key bytes are never returned or logged by this function.
func ensureKeypair(privKeyPath string) (wgPublicKey string, err error) {
	raw, err := os.ReadFile(privKeyPath)
	if err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("keyutil: read private key %s: %w", privKeyPath, err)
	}

	if os.IsNotExist(err) {
		return generateAndSaveKeypair(privKeyPath)
	}

	// Key already on disk: decode + derive public key.
	privBytes, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		return "", fmt.Errorf("keyutil: decode existing private key: %w", err)
	}
	if len(privBytes) != wgKeyLen {
		return "", fmt.Errorf("keyutil: existing private key wrong length: want %d, got %d", wgKeyLen, len(privBytes))
	}
	return wgDerivePublicKey(privBytes)
}

// generateAndSaveKeypair creates a fresh WG keypair, writes the private key
// to disk, and returns the public key. The private key file is created with
// mode 0600.
func generateAndSaveKeypair(privKeyPath string) (wgPublicKey string, err error) {
	privBytes := make([]byte, wgKeyLen)
	if _, err := rand.Read(privBytes); err != nil {
		return "", fmt.Errorf("keyutil: generate random bytes: %w", err)
	}

	// Curve25519 / WireGuard private key clamping (RFC 7748 §5).
	privBytes[0] &= 248
	privBytes[31] &= 127
	privBytes[31] |= 64

	dir := filepath.Dir(privKeyPath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", fmt.Errorf("keyutil: create key directory %s: %w", dir, err)
	}

	encoded := base64.StdEncoding.EncodeToString(privBytes)
	if err := os.WriteFile(privKeyPath, []byte(encoded), 0600); err != nil {
		return "", fmt.Errorf("keyutil: write private key: %w", err)
	}

	return wgDerivePublicKey(privBytes)
}

// wgDerivePublicKey computes the Curve25519 public key for the given 32-byte
// private key and returns it as a base64 string.
func wgDerivePublicKey(privBytes []byte) (string, error) {
	pub, err := curve25519.X25519(privBytes, curve25519.Basepoint)
	if err != nil {
		return "", fmt.Errorf("keyutil: curve25519 scalar mult: %w", err)
	}
	return base64.StdEncoding.EncodeToString(pub), nil
}
