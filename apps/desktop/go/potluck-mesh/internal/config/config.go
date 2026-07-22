// Package config loads and validates runtime configuration for potluck-mesh.
//
// Sources (in order of precedence, highest first):
//  1. CLI flags (--coordinator-url, --wg-private-key-path, --wg-public-key)
//  2. Environment variables (POTLUCK_COORDINATOR_URL, POTLUCK_WG_PRIVATE_KEY_PATH,
//     POTLUCK_WG_PUBLIC_KEY, POTLUCK_NODE_NAME, POTLUCK_LOG_FORMAT)
//  3. Defaults
//
// Privacy note: this package handles WG keypair paths and coordinator URLs only.
// It never logs key material, node content, or anything beyond mesh-layer metadata.
package config

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// LogFormat controls slog output format.
type LogFormat string

const (
	LogFormatJSON LogFormat = "json"
	LogFormatText LogFormat = "text"
)

// Config holds all runtime configuration for the sidecar.
type Config struct {
	// CoordinatorURL is the HTTP/WS base URL of the Potluck coordinator.
	// Example: "http://127.0.0.1:8400" or "https://coordinator.potluck.ai"
	CoordinatorURL string

	// CoordinatorToken is the shared secret presented to the coordinator
	// (X-Potluck-Coordinator-Token) on the signal WS and gated /mesh/* HTTP
	// calls. Empty = dev mode (coordinator runs tokenless / passthrough).
	CoordinatorToken string

	// WGPrivateKeyPath is the path to the WireGuard private key file.
	// If the file does not exist on first run, a new keypair is generated
	// and the private key is written here.
	WGPrivateKeyPath string

	// WGPublicKey is the base64-encoded WireGuard public key for this node.
	// Populated either from --wg-public-key flag / env var (for nodes that
	// already have a key) or derived after key generation on first run.
	WGPublicKey string

	// SigningPrivateKeyPath is the path to this node's Ed25519 signing
	// private key file, used ONLY for the DERP-token proof-of-possession
	// challenge (POST /mesh/derp-token nonce+signature — see
	// internal/signingkey). Distinct from WGPrivateKeyPath: the WG key is
	// Curve25519 (DH-only, cannot sign); this is a separate Ed25519 keypair.
	// Generated once and persisted (mode 0600) alongside the WG key, mirroring
	// LoadOrGenerateKeyPair's pattern. Never logged.
	SigningPrivateKeyPath string

	// NodeName is the human-readable name for this node (defaults to hostname).
	NodeName string

	// LogFormat controls whether slog emits JSON (production) or plaintext (dev).
	LogFormat LogFormat

	// Platform is detected at build time from runtime.GOOS.
	Platform string

	// STUNServers is the list of "host:port" STUN servers to use for external
	// endpoint discovery. Configurable via POTLUCK_STUN_SERVERS (comma-separated).
	// Defaults to stun.cloudflare.com:3478 + stun.l.google.com:19302.
	STUNServers []string

	// STUNRefreshInterval is how often the sidecar re-queries STUN to check
	// whether the external endpoint has changed. Default 5 minutes.
	STUNRefreshInterval time.Duration

	// DERPRelayURL is the URL of the DERP relay to use as a fallback when
	// direct UDP hole-punch fails. If empty, DERP is disabled.
	//
	// Supported schemes:
	//   "tcp+tls://host:port"       — TLS (production default)
	//   "tcp+insecure://host:port"  — cleartext TCP (dev / localhost testing)
	//   "tcp://host:port"           — alias for tcp+insecure
	//
	// Set via env POTLUCK_DERP_URL or --derp-url flag.
	DERPRelayURL string

	// LocalAPISocketPath overrides the default Unix socket (macOS/Linux) or
	// named pipe (Windows) path for the local IPC server. Leave empty to use
	// the platform default:
	//   macOS:   /tmp/potluck-mesh.sock
	//   Linux:   ${XDG_RUNTIME_DIR:-/run/user/$UID}/potluck-mesh.sock
	//   Windows: \\.\pipe\potluck-mesh
	//
	// Set via env POTLUCK_LOCAL_API_SOCKET or --local-api-socket flag.
	LocalAPISocketPath string
}

// Load parses flags and environment, returning a validated Config.
// Call this once from main(); do not call it from library code.
func Load() (*Config, error) {
	cfg := &Config{
		Platform: runtime.GOOS,
	}

	// ---- defaults ---- //
	defaultKeyPath := defaultPrivateKeyPath()
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "potluck-node"
	}

	// ---- flags ---- //
	flag.StringVar(&cfg.CoordinatorURL, "coordinator-url",
		envOr("POTLUCK_COORDINATOR_URL", "http://127.0.0.1:8400"),
		"Potluck coordinator base URL (env: POTLUCK_COORDINATOR_URL)")

	flag.StringVar(&cfg.CoordinatorToken, "coordinator-token",
		envOr("POTLUCK_COORDINATOR_TOKEN", ""),
		"Shared secret for the coordinator (X-Potluck-Coordinator-Token); empty = dev mode (env: POTLUCK_COORDINATOR_TOKEN)")

	flag.StringVar(&cfg.WGPrivateKeyPath, "wg-private-key-path",
		envOr("POTLUCK_WG_PRIVATE_KEY_PATH", defaultKeyPath),
		"Path to WireGuard private key file; generated on first run if absent (env: POTLUCK_WG_PRIVATE_KEY_PATH)")

	flag.StringVar(&cfg.WGPublicKey, "wg-public-key",
		envOr("POTLUCK_WG_PUBLIC_KEY", ""),
		"WireGuard public key (base64); if empty, derived from private key (env: POTLUCK_WG_PUBLIC_KEY)")

	flag.StringVar(&cfg.SigningPrivateKeyPath, "signing-key-path",
		envOr("POTLUCK_SIGNING_KEY_PATH", defaultSigningKeyPath()),
		"Path to the Ed25519 DERP-token proof-of-possession signing key file; generated on first run if absent (env: POTLUCK_SIGNING_KEY_PATH)")

	flag.StringVar(&cfg.NodeName, "node-name",
		envOr("POTLUCK_NODE_NAME", hostname),
		"Human-readable name for this node (env: POTLUCK_NODE_NAME)")

	logFmt := flag.String("log-format",
		envOr("POTLUCK_LOG_FORMAT", string(LogFormatText)),
		"Log output format: json or text (env: POTLUCK_LOG_FORMAT)")

	stunServers := flag.String("stun-servers",
		envOr("POTLUCK_STUN_SERVERS", ""),
		"Comma-separated STUN server list host:port (env: POTLUCK_STUN_SERVERS); defaults to Cloudflare + Google")

	stunRefresh := flag.Duration("stun-refresh-interval",
		envDuration("POTLUCK_STUN_REFRESH_INTERVAL", 5*time.Minute),
		"How often to re-query STUN for endpoint changes (env: POTLUCK_STUN_REFRESH_INTERVAL)")

	flag.StringVar(&cfg.DERPRelayURL, "derp-url",
		envOr("POTLUCK_DERP_URL", ""),
		"DERP relay URL for fallback routing (env: POTLUCK_DERP_URL); empty to disable")

	flag.StringVar(&cfg.LocalAPISocketPath, "local-api-socket",
		envOr("POTLUCK_LOCAL_API_SOCKET", ""),
		"Local IPC socket path; empty to use platform default (env: POTLUCK_LOCAL_API_SOCKET)")

	flag.Parse()

	if *stunServers != "" {
		parts := strings.Split(*stunServers, ",")
		for _, s := range parts {
			s = strings.TrimSpace(s)
			if s != "" {
				cfg.STUNServers = append(cfg.STUNServers, s)
			}
		}
	}
	cfg.STUNRefreshInterval = *stunRefresh

	// ---- normalize + validate ---- //
	cfg.CoordinatorURL = strings.TrimRight(cfg.CoordinatorURL, "/")
	if cfg.CoordinatorURL == "" {
		return nil, errors.New("--coordinator-url is required")
	}
	if !strings.HasPrefix(cfg.CoordinatorURL, "http://") && !strings.HasPrefix(cfg.CoordinatorURL, "https://") {
		return nil, fmt.Errorf("--coordinator-url must start with http:// or https://, got: %s", cfg.CoordinatorURL)
	}

	switch LogFormat(*logFmt) {
	case LogFormatJSON, LogFormatText:
		cfg.LogFormat = LogFormat(*logFmt)
	default:
		return nil, fmt.Errorf("--log-format must be 'json' or 'text', got: %s", *logFmt)
	}

	return cfg, nil
}

// SignalURL returns the WebSocket URL for the coordinator's /mesh/signal endpoint.
// It upgrades http → ws and https → wss.
func (c *Config) SignalURL() string {
	u := c.CoordinatorURL
	u = strings.Replace(u, "http://", "ws://", 1)
	u = strings.Replace(u, "https://", "wss://", 1)
	return u + "/mesh/signal"
}

// envOr returns the value of the named environment variable, or fallback
// if the variable is unset or empty.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// envDuration returns the duration stored in the named environment variable,
// or the fallback value if the variable is unset, empty, or not parseable.
func envDuration(key string, fallback time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return fallback
	}
	return d
}

// defaultPrivateKeyPath returns a sensible platform-specific default for
// where the WG private key file lives. The directory is created lazily by
// the keystore when the key is first generated.
func defaultPrivateKeyPath() string {
	switch runtime.GOOS {
	case "windows":
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			base = os.Getenv("APPDATA")
		}
		if base == "" {
			base = "C:\\ProgramData"
		}
		return base + "\\Potluck\\mesh\\wg.key"
	case "darwin":
		// LaunchDaemon runs as a dedicated _potluck system user.
		// /var/lib/potluck is the preferred system-level state dir on macOS
		// for a background service (no user home available at boot).
		// Fall back to $HOME when running interactively (dev mode).
		if home := os.Getenv("HOME"); home != "" {
			return home + "/Library/Application Support/Potluck/mesh/wg.key"
		}
		return "/var/lib/potluck/mesh/wg.key"
	default: // linux + others
		// systemd system service runs as dedicated 'potluck' user.
		if home := os.Getenv("HOME"); home != "" {
			return home + "/.local/state/potluck/mesh/wg.key"
		}
		return "/var/lib/potluck/mesh/wg.key"
	}
}

// defaultSigningKeyPath returns the default path for the Ed25519
// proof-of-possession signing key, sited next to the default WG key path
// (same directory, "signing.key" filename). Computed independently of any
// --wg-private-key-path override so the flag default is stable at flag-
// registration time (flags are declared before Parse() runs).
func defaultSigningKeyPath() string {
	return filepath.Join(filepath.Dir(defaultPrivateKeyPath()), "signing.key")
}
