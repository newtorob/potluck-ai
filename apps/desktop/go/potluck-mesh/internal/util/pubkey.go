// Package util provides small helpers shared across multiple internal packages.
//
// Nothing in this package may import other internal packages - it sits at the
// bottom of the dependency graph to avoid import cycles.
package util

import "fmt"

// TruncatePubkey returns the first 8 characters of a base64-encoded WireGuard
// public key, followed by "...". Used throughout for safe log output - enough
// to correlate lines, not enough to reconstruct the key.
//
// If the input is shorter than 8 characters it is returned as-is (without the
// suffix) so the function is always safe to call.
func TruncatePubkey(pubkey string) string {
	if len(pubkey) > 8 {
		return pubkey[:8] + "..."
	}
	return pubkey
}

// TruncatePubkeyBytes returns the first 8 hex characters of a raw 32-byte
// WireGuard public key for safe log output.
func TruncatePubkeyBytes(key [32]byte) string {
	// 4 bytes → 8 hex chars
	return fmt.Sprintf("%x...", key[:4])
}
