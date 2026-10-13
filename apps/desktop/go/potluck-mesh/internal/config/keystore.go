package config

import (
	"encoding/base64"
	"fmt"
	"log/slog"
	"os"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

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
	if len(privBytes) != util.WGKeyLen {
		return fmt.Errorf("keystore: private key wrong length: want %d bytes, got %d", util.WGKeyLen, len(privBytes))
	}

	pubEncoded, err := util.WGDerivePublicKey(privBytes)
	if err != nil {
		return fmt.Errorf("keystore: derive public key: %w", err)
	}

	// If a --wg-public-key flag was supplied, verify it matches the key on disk.
	if cfg.WGPublicKey != "" && cfg.WGPublicKey != pubEncoded {
		return fmt.Errorf("keystore: --wg-public-key flag (%s) does not match the public key derived from %s (%s)",
			cfg.WGPublicKey, cfg.WGPrivateKeyPath, pubEncoded)
	}

	cfg.WGPublicKey = pubEncoded
	// Log only the truncated prefix - never the full public key in logs.
	slog.Debug("loaded existing WG keypair", "wg_public_key_prefix", util.TruncatePubkey(pubEncoded))
	return nil
}

func generateAndSave(cfg *Config) error {
	pubEncoded, err := util.WGGenerateAndSavePrivateKey(cfg.WGPrivateKeyPath)
	if err != nil {
		return fmt.Errorf("keystore: %w", err)
	}
	cfg.WGPublicKey = pubEncoded

	slog.Info("keypair generated, register this node via POST /mesh/enroll",
		"wg_public_key", pubEncoded,
		"private_key_path", cfg.WGPrivateKeyPath,
	)
	return nil
}
