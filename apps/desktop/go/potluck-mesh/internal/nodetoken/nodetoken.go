// Package nodetoken manages the per-node bearer credential (`node_token`)
// minted once by the coordinator at POST /mesh/enroll and used thereafter as
// `Authorization: Bearer <node_token>` on every machine-plumbing call
// (heartbeat, peers, derp-token, delete-node, and the /mesh/signal WS
// upgrade) — see coordinator/middleware/node_auth.py's wire-contract doc,
// which this package's persistence format must match on the client side.
//
// This package is intentionally narrow: it only reads/writes the raw token
// to/from disk. Holding the LIVE value used by in-flight requests, and
// propagating updates after a fresh enroll, is internal/coordapi.Client's
// job (SetNodeToken/NodeToken) — main.go wires coordapi.Client as the single
// shared source of truth so the signal client and the DERP-token fetcher
// pick up a freshly-minted token automatically (see cmd/potluck-mesh/main.go).
//
// Storage mirrors internal/signingkey's convention exactly: mode 0600,
// parent directory created at 0700, sited in the SAME directory as the WG
// private key (never a secret-adjacent-but-differently-permissioned path).
// Unlike the WG/signing keys, a missing file is never an error and never
// triggers local generation — the node_token can ONLY come from the
// coordinator's enroll response; this package never mints one.
package nodetoken

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// fileName is the sibling filename used next to the WG private key.
const fileName = "node.token"

// Path returns the node.token path for a given WG private-key path: same
// directory, "node.token" filename. Mirrors config.defaultSigningKeyPath's
// sibling-file convention.
func Path(wgPrivKeyPath string) string {
	return filepath.Join(filepath.Dir(wgPrivKeyPath), fileName)
}

// Load reads the node_token from path. A missing file is NOT an error — it
// just means this node hasn't completed a POST /mesh/enroll since this
// feature shipped (fresh install awaiting /pair, or an already-enrolled node
// that predates node_token support and is still relying on the legacy
// coordinator token). Returns ("", nil) in that case. Never logs the token.
func Load(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", fmt.Errorf("nodetoken: read %s: %w", path, err)
	}
	return strings.TrimSpace(string(raw)), nil
}

// Save persists token to path at mode 0600 (parent directory created at
// 0700 if it doesn't already exist — it normally does, since the WG key
// lives there too). Call this exactly once per enroll, immediately after a
// successful POST /mesh/enroll response — the coordinator shows the raw
// value exactly once, so a save failure here means falling back to the
// legacy coordinator-token credential until the next successful (re-)enroll.
func Save(path, token string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("nodetoken: create directory %s: %w", dir, err)
	}
	if err := os.WriteFile(path, []byte(token), 0600); err != nil {
		return fmt.Errorf("nodetoken: write %s: %w", path, err)
	}
	return nil
}

// Clear removes the node_token file. Idempotent — a missing file is not an
// error. Called from /unpair alongside wiping the WG private key, so a
// subsequent /pair starts clean and persists a completely fresh node_token
// rather than leaving a stale one on disk that no longer matches the new
// enrollment.
func Clear(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("nodetoken: remove %s: %w", path, err)
	}
	return nil
}
