// Package runtimesettings persists a small set of user-overridable runtime
// settings for the mesh sidecar - currently just the coordinator URL - to a
// JSON file sited next to the WG private key. Storage mirrors
// internal/nodetoken's and internal/signingkey's convention exactly: mode
// 0600, parent directory created at 0700, same directory as wg.key.
//
// Why this exists: internal/config.Config.CoordinatorURL is resolved once at
// startup from a CLI flag / env var. The macOS LaunchDaemon (and the
// Linux/Windows service equivalents) ship with a STATIC plist/unit baked
// with the production coordinator URL, so a user who wants to self-host or
// point at a LAN coordinator has no flag to change post-install. PATCH
// /settings on the local IPC socket (see internal/localapi's
// handleSettingsPatch) lets Electron write an override here; main.go loads
// it at startup - before constructing anything coordinator-facing - so a
// saved override always wins over the flag/env default and survives daemon
// restarts.
//
// Privacy: this package only ever handles a coordinator URL, never key
// material, tokens, or content. Never logs anything itself (the caller may
// choose to).
package runtimesettings

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// fileName is the sibling filename used next to the WG private key.
const fileName = "settings.json"

// Settings is the full set of user-overridable runtime settings persisted by
// this package. An empty field means "no override" - the caller's flag/env
// default applies.
type Settings struct {
	// CoordinatorURL overrides config.Config.CoordinatorURL when non-empty.
	// Always normalized (no trailing slash) before being stored here - see
	// the validation performed by internal/localapi's handleSettingsPatch.
	CoordinatorURL string `json:"coordinator_url,omitempty"`
}

// Path returns the settings.json path for a given WG private-key path: same
// directory, "settings.json" filename. Mirrors nodetoken.Path /
// config.defaultSigningKeyPath's sibling-file convention.
func Path(wgPrivKeyPath string) string {
	return filepath.Join(filepath.Dir(wgPrivKeyPath), fileName)
}

// Load reads persisted settings from path. A missing file is NOT an error -
// it just means no override has ever been saved; returns the zero value
// (every field "no override"). A corrupt file (rare - nothing else should
// ever write here) IS surfaced as an error so main.go can log it clearly
// rather than silently ignoring a stuck bad override.
func Load(path string) (Settings, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return Settings{}, nil
		}
		return Settings{}, fmt.Errorf("runtimesettings: read %s: %w", path, err)
	}
	var s Settings
	if err := json.Unmarshal(raw, &s); err != nil {
		return Settings{}, fmt.Errorf("runtimesettings: decode %s: %w", path, err)
	}
	return s, nil
}

// Save persists settings to path at mode 0600 (parent directory created at
// 0700 if it doesn't already exist - it normally does, since the WG key
// lives there too). Overwrites any previous contents; callers wanting to
// change just one field should Load first, mutate, then Save the whole
// struct (mirrors internal/localapi's handleSettingsPatch, which currently
// has exactly one field to manage).
func Save(path string, s Settings) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("runtimesettings: create directory %s: %w", dir, err)
	}
	raw, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("runtimesettings: encode: %w", err)
	}
	if err := os.WriteFile(path, raw, 0600); err != nil {
		return fmt.Errorf("runtimesettings: write %s: %w", path, err)
	}
	return nil
}
