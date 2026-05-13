package localapi

import (
	"encoding/base64"
	"fmt"
	"os"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

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
		pubKey, genErr := util.WGGenerateAndSavePrivateKey(privKeyPath)
		if genErr != nil {
			return "", fmt.Errorf("keyutil: %w", genErr)
		}
		return pubKey, nil
	}

	// Key already on disk: decode + derive public key.
	privBytes, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		return "", fmt.Errorf("keyutil: decode existing private key: %w", err)
	}
	if len(privBytes) != util.WGKeyLen {
		return "", fmt.Errorf("keyutil: existing private key wrong length: want %d, got %d", util.WGKeyLen, len(privBytes))
	}
	pubKey, err := util.WGDerivePublicKey(privBytes)
	if err != nil {
		return "", fmt.Errorf("keyutil: %w", err)
	}
	return pubKey, nil
}
