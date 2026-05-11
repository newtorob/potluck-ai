package config

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"golang.org/x/crypto/curve25519"
)

const wgKeyLen = 32

// LoadOrGenerateKeyPair reads the private key from cfg.WGPrivateKeyPath.
// If the file does not exist, it generates a fresh WireGuard keypair,
// writes the private key to the path (mode 0600), logs the public key for
// the operator to register via POST /mesh/enroll, and updates cfg.WGPublicKey.
//
// Private key is stored as standard base64. Public key is derived via
// Curve25519 scalar multiplication (the WireGuard key derivation algorithm).
//
// Never logs or prints the private key.
func LoadOrGenerateKeyPair(cfg *Config) error {
	raw, err := os.ReadFile(cfg.WGPrivateKeyPath)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("keystore: read private key: %w", err)
	}

	if os.IsNotExist(err) {
		return generateAndSave(cfg)
	}

	// Existing key: decode and derive the public key.
	privBytes, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		return fmt.Errorf("keystore: decode private key: %w", err)
	}
	if len(privBytes) != wgKeyLen {
		return fmt.Errorf("keystore: private key wrong length: want %d bytes, got %d", wgKeyLen, len(privBytes))
	}

	pubEncoded, err := derivePublicKey(privBytes)
	if err != nil {
		return fmt.Errorf("keystore: derive public key: %w", err)
	}

	// If a --wg-public-key flag was supplied, verify it matches the key on disk.
	if cfg.WGPublicKey != "" && cfg.WGPublicKey != pubEncoded {
		return fmt.Errorf("keystore: --wg-public-key flag (%s) does not match the public key derived from %s (%s)",
			cfg.WGPublicKey, cfg.WGPrivateKeyPath, pubEncoded)
	}

	cfg.WGPublicKey = pubEncoded
	slog.Debug("loaded existing WG keypair", "wg_public_key", pubEncoded)
	return nil
}

func generateAndSave(cfg *Config) error {
	privBytes := make([]byte, wgKeyLen)
	if _, err := rand.Read(privBytes); err != nil {
		return fmt.Errorf("keystore: generate random bytes: %w", err)
	}

	// WireGuard private key clamping (RFC 7748 §5 / Curve25519):
	privBytes[0] &= 248
	privBytes[31] &= 127
	privBytes[31] |= 64

	dir := filepath.Dir(cfg.WGPrivateKeyPath)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("keystore: create key directory %s: %w", dir, err)
	}

	encoded := base64.StdEncoding.EncodeToString(privBytes)
	if err := os.WriteFile(cfg.WGPrivateKeyPath, []byte(encoded), 0600); err != nil {
		return fmt.Errorf("keystore: write private key: %w", err)
	}

	pubEncoded, err := derivePublicKey(privBytes)
	if err != nil {
		return fmt.Errorf("keystore: derive public key after generation: %w", err)
	}
	cfg.WGPublicKey = pubEncoded

	slog.Info("keypair generated — register this node via POST /mesh/enroll",
		"wg_public_key", pubEncoded,
		"private_key_path", cfg.WGPrivateKeyPath,
	)
	return nil
}

// derivePublicKey computes the Curve25519 public key for a given private key.
// This is the same derivation WireGuard uses internally (Diffie-Hellman base
// point multiplication per RFC 7748).
func derivePublicKey(privBytes []byte) (string, error) {
	pub, err := curve25519.X25519(privBytes, curve25519.Basepoint)
	if err != nil {
		return "", fmt.Errorf("curve25519 scalar mult: %w", err)
	}
	return base64.StdEncoding.EncodeToString(pub), nil
}
