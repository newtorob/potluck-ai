// potluck-mesh — Potluck AI mesh sidecar.
//
// Embeds a userspace WireGuard device (wg-go) and maintains a persistent
// signaling WebSocket to the Potluck coordinator. On welcome, configures the
// WG tunnel with the full household roster and assigns the mesh IP to the TUN
// interface. Reconnects automatically with exponential backoff.
//
// Week 3 additions:
//   - STUN client: resolves the external UDP endpoint on startup and refreshes
//     every POTLUCK_STUN_REFRESH_INTERVAL (default 5 minutes).
//   - endpoint_update: sent to the coordinator when STUN discovers a new or
//     changed external endpoint.
//   - WG peer configuration: on welcome, SetPeers is called with the full
//     household roster; on roster_update, AddPeer / UpdatePeerEndpoint /
//     RemovePeer are called for each delta.
//   - Mesh IP assignment: SetMeshIP is called on welcome to bring the TUN
//     interface address up.
//   - In-memory peer cache: maps node_id → RosterPeer so roster_update.removed
//     (which only carries node_id) can look up the WG public key for removal.
//
// Not yet implemented: UDP hole-punch (week 4), DERP relay (week 5), local
// IPC for Electron, /etc/hosts writer.
//
// Run:
//
//	potluck-mesh --coordinator-url http://127.0.0.1:8400
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/config"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/stun"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

func main() {
	os.Exit(run())
}

func run() int {
	// ---- config + logging ---- //
	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "potluck-mesh: config error: %v\n", err)
		return 1
	}

	setupLogging(cfg)
	slog.Info("potluck-mesh starting",
		"coordinator", cfg.CoordinatorURL,
		"platform", cfg.Platform,
		"node_name", cfg.NodeName,
		"stun_refresh", cfg.STUNRefreshInterval,
	)

	// ---- keypair ---- //
	if err := config.LoadOrGenerateKeyPair(cfg); err != nil {
		slog.Error("keypair init failed", "err", err)
		return 1
	}

	// ---- root context, wired to OS signals ---- //
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// ---- WireGuard tunnel ---- //
	privKeyRaw, err := os.ReadFile(cfg.WGPrivateKeyPath)
	if err != nil {
		slog.Error("read private key file", "path", cfg.WGPrivateKeyPath, "err", err)
		return 1
	}
	privKeyBase64 := strings.TrimSpace(string(privKeyRaw))

	// Start WG device with no mesh IP yet; SetMeshIP is called from OnWelcome.
	tun, err := tunnel.New(privKeyBase64, "")
	if err != nil {
		slog.Error("tunnel init failed",
			"err", err,
			"hint_linux", "ensure CAP_NET_ADMIN is set: sudo setcap cap_net_admin+ep ./potluck-mesh",
			"hint_mac", "no extra privileges required for utun on macOS",
			"hint_windows", "wintun.dll must be installed; run the installer once as admin",
		)
		return 1
	}
	defer tun.Close()

	// ---- peer state cache ---- //
	ps := peerstate.New()

	// ---- STUN resolver ---- //
	resolver := stun.NewResolver(cfg.STUNServers)

	// ---- signaling client ---- //
	handler := newRosterHandler(tun, ps)
	client := msignal.NewClient(cfg.SignalURL(), cfg.WGPublicKey, handler)

	// ---- STUN goroutine ---- //
	// Performs an initial STUN resolve and then refreshes on the configured
	// interval, sending endpoint_update whenever the endpoint changes.
	// Runs concurrently with the signal client.
	go runSTUNLoop(ctx, resolver, client, cfg.STUNRefreshInterval)

	// ---- signaling client (blocks until ctx is cancelled) ---- //
	slog.Info("connecting to coordinator signal channel", "url", cfg.SignalURL())
	if err := client.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("signal client exited with error", "err", err)
		return 1
	}

	slog.Info("potluck-mesh shutdown complete")
	return 0
}

// runSTUNLoop runs an initial STUN binding request then refreshes on interval.
// On each successful resolve, if the endpoint changed, it calls
// client.SendEndpointUpdate. Errors are logged and retried; they never crash.
//
// The initial resolve uses a short exponential backoff so that startup isn't
// blocked if the STUN server is temporarily unreachable.
func runSTUNLoop(
	ctx context.Context,
	resolver *stun.Resolver,
	client *msignal.Client,
	interval time.Duration,
) {
	var lastEndpoint string

	// Initial resolve with backoff.
	resolveWithBackoff := func() string {
		backoff := 500 * time.Millisecond
		const maxBackoff = 30 * time.Second
		for attempt := 1; ; attempt++ {
			ep, err := resolver.Resolve(ctx)
			if err == nil {
				return ep
			}
			if ctx.Err() != nil {
				return ""
			}
			slog.Warn("stun: resolve failed, will retry",
				"attempt", attempt,
				"backoff_ms", backoff.Milliseconds(),
				"err", err,
			)
			select {
			case <-ctx.Done():
				return ""
			case <-time.After(backoff):
			}
			if backoff*2 <= maxBackoff {
				backoff *= 2
			} else {
				backoff = maxBackoff
			}
		}
	}

	// First attempt before the refresh ticker so we send endpoint_update
	// as soon as the signaling connection comes up.
	ep := resolveWithBackoff()
	if ctx.Err() != nil {
		return
	}
	if ep != "" && ep != lastEndpoint {
		slog.Info("stun: initial endpoint discovered", "endpoint", ep)
		lastEndpoint = ep
		client.SendEndpointUpdate(ep)
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return

		case <-ticker.C:
			ep, err := resolver.Resolve(ctx)
			if err != nil {
				slog.Warn("stun: refresh failed", "err", err)
				continue
			}
			if ep == lastEndpoint {
				slog.Debug("stun: endpoint unchanged", "endpoint", ep)
				continue
			}
			slog.Info("stun: endpoint changed", "old", lastEndpoint, "new", ep)
			lastEndpoint = ep
			client.SendEndpointUpdate(ep)
		}
	}
}

// rosterHandler receives welcome + roster update events from the signal client
// and applies them to the WireGuard tunnel.
type rosterHandler struct {
	tun *tunnel.Tunnel
	ps  *peerstate.Store
}

func newRosterHandler(tun *tunnel.Tunnel, ps *peerstate.Store) *rosterHandler {
	return &rosterHandler{tun: tun, ps: ps}
}

// OnWelcome is called once per signaling connection after a successful hello.
// It:
//  1. Assigns the mesh IP to the TUN interface.
//  2. Populates the peer state cache from the full roster.
//  3. Calls SetPeers to atomically configure all WG peers.
func (h *rosterHandler) OnWelcome(w msignal.Welcome) {
	slog.Info("mesh welcome received",
		"node_id", w.NodeID,
		"mesh_ip", w.MeshIP,
		"household_id", w.HouseholdID,
		"roster_size", len(w.Roster),
	)

	// 1. Assign mesh IP.
	if w.MeshIP != "" {
		if err := h.tun.SetMeshIP(w.MeshIP); err != nil {
			slog.Error("tunnel: SetMeshIP failed", "mesh_ip", w.MeshIP, "err", err)
			// Non-fatal: WG peers can still be configured even if the
			// interface address assignment fails (e.g. missing ip/ifconfig).
		}
	}

	// 2. Populate peer cache from the roster (excluding ourselves).
	h.ps.Reset(w.Roster)

	// 3. Build WG Peer list and apply.
	peers := rosterToPeers(w.Roster)
	if err := h.tun.SetPeers(peers); err != nil {
		slog.Error("tunnel: SetPeers failed on welcome", "err", err)
		return
	}

	// Log each peer for observability (mesh_ip + endpoint only).
	for _, peer := range w.Roster {
		ep := "<none>"
		if peer.LastEndpoint != nil {
			ep = *peer.LastEndpoint
		}
		slog.Info("roster peer configured",
			"node_id", peer.NodeID,
			"mesh_ip", peer.MeshIP,
			"endpoint", ep,
		)
	}
}

// OnRosterUpdate applies incremental peer changes to the WireGuard tunnel and
// the in-memory peer cache.
func (h *rosterHandler) OnRosterUpdate(u msignal.RosterUpdate) {
	// Added peers: insert into cache + configure WG peer.
	for _, p := range u.Added {
		h.ps.Upsert(p)
		peer := rosterPeerToTunnelPeer(p)
		if err := h.tun.AddPeer(peer); err != nil {
			slog.Error("tunnel: AddPeer failed",
				"node_id", p.NodeID,
				"mesh_ip", p.MeshIP,
				"err", err,
			)
		}
	}

	// Updated peers: update the endpoint (and cache) if endpoint changed.
	// The public key is stable; only last_endpoint changes in practice for
	// a roster_update triggered by endpoint_update.
	for _, p := range u.Updated {
		h.ps.Upsert(p)
		if p.LastEndpoint != nil && *p.LastEndpoint != "" {
			if err := h.tun.UpdatePeerEndpoint(p.WGPublicKey, *p.LastEndpoint); err != nil {
				slog.Error("tunnel: UpdatePeerEndpoint failed",
					"node_id", p.NodeID,
					"err", err,
				)
			}
		} else {
			// Endpoint cleared (peer went offline or re-enrolled without STUN yet).
			// We keep the peer in WG but log it; the peer can still initiate.
			slog.Info("roster update: peer endpoint cleared",
				"node_id", p.NodeID,
				"mesh_ip", p.MeshIP,
			)
		}
	}

	// Removed peers: look up pubkey from cache, then remove from WG.
	for _, nodeID := range u.Removed {
		cached, ok := h.ps.Remove(nodeID)
		if !ok {
			slog.Warn("roster remove: unknown node_id (not in local cache)",
				"node_id", nodeID,
			)
			continue
		}
		if err := h.tun.RemovePeer(cached.WGPublicKey); err != nil {
			slog.Error("tunnel: RemovePeer failed",
				"node_id", nodeID,
				"err", err,
			)
		}
	}
}

// rosterToPeers converts a []RosterPeer to []tunnel.Peer for SetPeers.
func rosterToPeers(roster []msignal.RosterPeer) []tunnel.Peer {
	peers := make([]tunnel.Peer, 0, len(roster))
	for _, p := range roster {
		peers = append(peers, rosterPeerToTunnelPeer(p))
	}
	return peers
}

// rosterPeerToTunnelPeer converts a single RosterPeer to a tunnel.Peer.
// AllowedIPs is always "<mesh_ip>/32" — traffic to the peer's mesh address
// only, nothing broader.
// PersistentKeepalive is 0 (off) per the design doc mandate.
func rosterPeerToTunnelPeer(p msignal.RosterPeer) tunnel.Peer {
	ep := ""
	if p.LastEndpoint != nil {
		ep = *p.LastEndpoint
	}
	return tunnel.Peer{
		PublicKey:           p.WGPublicKey,
		AllowedIPs:          []string{p.MeshIP + "/32"},
		Endpoint:            ep,
		PersistentKeepalive: 0, // TODO(keepalive-toggle): user-configurable opt-in
	}
}

// setupLogging configures slog based on the log format in config.
func setupLogging(cfg *config.Config) {
	var handler slog.Handler
	opts := &slog.HandlerOptions{Level: slog.LevelDebug}
	switch cfg.LogFormat {
	case config.LogFormatJSON:
		handler = slog.NewJSONHandler(os.Stderr, opts)
	default:
		handler = slog.NewTextHandler(os.Stderr, opts)
	}
	slog.SetDefault(slog.New(handler))
}
