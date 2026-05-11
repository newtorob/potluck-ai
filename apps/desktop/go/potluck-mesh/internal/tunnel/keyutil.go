package tunnel

import (
	"encoding/base64"
	"encoding/hex"
	"fmt"
)

// base64ToHex converts a standard base64-encoded string to lowercase hex.
// The WireGuard UAPI configuration protocol expects keys as 64-char lowercase hex.
func base64ToHex(b64 string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return "", fmt.Errorf("base64 decode: %w", err)
	}
	return hex.EncodeToString(raw), nil
}
