// potluck-derp - Potluck DERP relay server.
//
// Listens on a TLS (or cleartext-dev) TCP port and relays WireGuard encrypted
// packets between authenticated peers. See internal/relay for the wire format.
//
// Usage (dev):
//
//	potluck-derp --insecure-cleartext
//
// Usage (production):
//
//	potluck-derp --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
//
// Environment variables (all optional - flags take precedence):
//
//	POTLUCK_DERP_PORT        TCP port for the relay (default 8443)
//	POTLUCK_DERP_HEALTH_PORT HTTP port for /health   (default 8444)
//	POTLUCK_DERP_REGION      Region identifier        (default "ord")
//	POTLUCK_DERP_JWT_KEY     HMAC signing key shared with the coordinator (required)
//	POTLUCK_DERP_MAX_CLIENTS Maximum concurrent connections (default 10000)
package main

import (
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/potluck-ai/potluck-derp/internal/relay"
)

func main() {
	os.Exit(run())
}

func run() int {
	// ---- flags ---- //
	var (
		insecure   = flag.Bool("insecure-cleartext", false, "Skip TLS (for localhost dev only)")
		tlsCert    = flag.String("tls-cert", "", "Path to TLS certificate file")
		tlsKey     = flag.String("tls-key", "", "Path to TLS key file")
		port       = flag.Int("port", envInt("POTLUCK_DERP_PORT", 8443), "Relay TCP port (env: POTLUCK_DERP_PORT)")
		healthPort = flag.Int("health-port", envInt("POTLUCK_DERP_HEALTH_PORT", 8444), "Health HTTP port (env: POTLUCK_DERP_HEALTH_PORT)")
		region     = flag.String("region", envOr("POTLUCK_DERP_REGION", "ord"), "DERP region identifier (env: POTLUCK_DERP_REGION)")
		maxClients = flag.Int("max-clients", envInt("POTLUCK_DERP_MAX_CLIENTS", 10_000), "Max concurrent connections (env: POTLUCK_DERP_MAX_CLIENTS)")
		logJSON    = flag.Bool("log-json", false, "Emit JSON logs")
	)
	flag.Parse()

	// ---- logging ---- //
	var logHandler slog.Handler
	opts := &slog.HandlerOptions{Level: slog.LevelInfo}
	if *logJSON {
		logHandler = slog.NewJSONHandler(os.Stderr, opts)
	} else {
		logHandler = slog.NewTextHandler(os.Stderr, opts)
	}
	slog.SetDefault(slog.New(logHandler))

	// ---- JWT signing key ---- //
	jwtKey := []byte(os.Getenv("POTLUCK_DERP_JWT_KEY"))
	if len(jwtKey) == 0 {
		slog.Error("POTLUCK_DERP_JWT_KEY env var is required")
		return 1
	}

	// ---- build the relay ---- //
	r := relay.New(relay.Config{
		Region:     *region,
		JWTKey:     jwtKey,
		MaxClients: *maxClients,
	})

	// ---- listener ---- //
	listenAddr := fmt.Sprintf(":%d", *port)
	var ln net.Listener
	var listenErr error

	if *insecure {
		slog.Warn("derp: RUNNING IN INSECURE CLEARTEXT MODE, dev only", "addr", listenAddr)
		ln, listenErr = net.Listen("tcp", listenAddr)
	} else {
		if *tlsCert == "" || *tlsKey == "" {
			slog.Error("TLS requires --tls-cert and --tls-key (or use --insecure-cleartext for dev)")
			return 1
		}
		cert, err := tls.LoadX509KeyPair(*tlsCert, *tlsKey)
		if err != nil {
			slog.Error("TLS keypair load failed", "err", err)
			return 1
		}
		tlsCfg := &tls.Config{
			Certificates: []tls.Certificate{cert},
			MinVersion:   tls.VersionTLS12,
		}
		ln, listenErr = tls.Listen("tcp", listenAddr, tlsCfg)
	}
	if listenErr != nil {
		slog.Error("listen failed", "addr", listenAddr, "err", listenErr)
		return 1
	}
	defer ln.Close()

	slog.Info("derp: relay listening",
		"addr", listenAddr,
		"region", *region,
		"tls", !*insecure,
	)

	// ---- health endpoint ---- //
	healthAddr := fmt.Sprintf(":%d", *healthPort)
	healthMux := http.NewServeMux()
	healthMux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// Encode error is non-actionable for an HTTP health handler writing to ResponseWriter.
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"status":                "ok",
			"connected_clients":     r.ConnectedClients(),
			"total_bytes_forwarded": r.TotalBytesForwarded(),
		})
	})
	go func() {
		if err := http.ListenAndServe(healthAddr, healthMux); err != nil && err != http.ErrServerClosed {
			slog.Error("health server failed", "err", err)
		}
	}()
	slog.Info("derp: health endpoint", "addr", healthAddr)

	// ---- accept loop ---- //
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				// Listener closed (shutdown), exit silently.
				return
			}
			go r.HandleConn(conn)
		}
	}()

	// ---- wait for shutdown signal ---- //
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	sig := <-sigCh
	slog.Info("derp: received signal, shutting down", "signal", sig)

	r.Shutdown()
	ln.Close()

	slog.Info("derp: shutdown complete")
	return 0
}

// envOr returns the env var value or fallback.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// envInt parses an int env var or returns the fallback.
func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}
