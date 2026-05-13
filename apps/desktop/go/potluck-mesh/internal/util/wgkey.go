package util

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/crypto/curve25519"
)

const WGKeyLen = 32

// WGDerivePublicKey computes the Curve25519 public key for a given 32-byte
// WireGuard private key and returns it as a standard-base64 string.
// This is the same derivation WireGuard uses (RFC 7748 §5).
func WGDerivePublicKey(privBytes []byte) (string, error) {
	pub, err := curve25519.X25519(privBytes, curve25519.Basepoint)
	if err != nil {
		return "", fmt.Errorf("curve25519 scalar mult: %w", err)
	}
	return base64.StdEncoding.EncodeToString(pub), nil
}

// WGClampPrivateKey applies the Curve25519 / WireGuard clamping bits to a
// raw 32-byte slice in place (RFC 7748 §5).
func WGClampPrivateKey(b []byte) {
	b[0] &= 248
	b[31] &= 127
	b[31] |= 64
}

// WGGenerateAndSavePrivateKey generates a fresh clamped Curve25519 private key,
// writes it as standard-base64 to privKeyPath (mode 0600, parent dirs created
// at 0700), and returns the base64-encoded public key.
// The private key bytes are never returned or logged.
func WGGenerateAndSavePrivateKey(privKeyPath string) (wgPublicKey string, err error) {
	privBytes := make([]byte, WGKeyLen)
	if _, err := rand.Read(privBytes); err != nil {
		return "", fmt.Errorf("generate random bytes: %w", err)
	}
	WGClampPrivateKey(privBytes)

	dir := filepath.Dir(privKeyPath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", fmt.Errorf("create key directory %s: %w", dir, err)
	}

	encoded := base64.StdEncoding.EncodeToString(privBytes)
	if err := os.WriteFile(privKeyPath, []byte(encoded), 0600); err != nil {
		return "", fmt.Errorf("write private key to %s: %w", privKeyPath, err)
	}

	return WGDerivePublicKey(privBytes)
}
