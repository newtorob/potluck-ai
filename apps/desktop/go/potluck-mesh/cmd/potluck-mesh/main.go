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
// Week 4 additions:
//   - UDP hole-punch coordination: on welcome/roster_update, sends
//     request_punch to the coordinator for each peer that has a STUN endpoint
//     but no fresh WG handshake yet. Throttled to once per 30s per peer.
//   - punch_coord dispatch: OnPunchCoord calls UpdatePeerEndpoint so both
//     sides configure each other simultaneously, letting WG's handshake-init
//     retries punch through the NAT.
//   - Reachability tracking: background goroutine queries wg-go's UAPI every
//     30s and logs state transitions (unreachable→reachable, reachable→unreachable).
//   - Windows mesh IP assignment: implemented via netsh (see ifaddr_windows.go).
//
// Week 5 additions:
//   - DERP relay client (internal/derp): TCP connection to the relay,
//     JWT auth, reconnect with exponential backoff, Send/Receive framing.
//   - DERP fallback trigger: when a peer's last handshake is older than
//     derpFallbackThreshold (30s), outbound packets to that peer are sent
//     via the relay instead of (or in addition to) direct UDP.
//   - JWT fetcher: hits the coordinator's POST /mesh/derp-token endpoint
//     to obtain a short-lived HMAC-signed token for DERP auth.
//
// Week 5b additions (DERP last-mile):
//   - DerpBind (internal/derp/bind.go): custom conn.Bind that intercepts
//     wg-go's send/receive paths. Inbound DERP packets are injected via
//     Inject(); outbound to syntheticEndpoints are dispatched to the relay.
//   - Automatic DERP fallback: runReachabilityLoop flips stale peers (no
//     fresh handshake for derpFallbackThreshold) to DERP routing via
//     DerpBind.SetPeerRouting + tunnel.UpdatePeerEndpoint with the synthetic
//     endpoint. Peers that recover (fresh handshake seen) are flipped back.
//
// Week 6 additions:
//   - Event bus (internal/events): in-process pub/sub that fans out mesh-state
//     changes to the IPC server's WS /events subscribers.
//   - Local IPC server (internal/localapi): HTTP+WS over Unix socket (macOS/Linux)
//     or named pipe (Windows). Endpoints: GET /status, GET /peers, POST /pair,
//     POST /unpair, WS /events.
//   - Coordinator API client (internal/coordapi): typed wrapper for POST /mesh/enroll,
//     called from POST /pair.
//   - Roster handler now publishes events to the bus on OnWelcome / OnRosterUpdate
//     so WS /events subscribers see live roster changes.
//   - Reachability loop publishes peer_added / peer_removed on state transitions.
//
// Not yet implemented: /etc/hosts writer (week 7).
//
// Run:
//
//	potluck-mesh --coordinator-url http://127.0.0.1:8400
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"github.com/potluck-ai/potluck-mesh/internal/config"
	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/stun"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// punchThrottlePeriod is the minimum interval between request_punch calls for
// the same peer. Prevents coordinator spam when a peer's endpoint flaps or
// when the welcome/roster loop fires rapidly.
const punchThrottlePeriod = 30 * time.Second

// handshakeFreshWindow is the age threshold below which a WG last-handshake
// is considered "fresh" (i.e. the peer is reachable and hole-punch is not
// needed). Matches the design doc's 3-minute figure.
const handshakeFreshWindow = 3 * time.Minute

// reachabilityCheckInterval controls how often the background goroutine polls
// wg-go's UAPI for peer handshake times.
const reachabilityCheckInterval = 30 * time.Second

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

	// ---- DERP bind (optional, constructed before tunnel) ---- //
	// DerpBind must wrap the inner conn.Bind before the WG device is created
	// so that device.NewDevice receives it. We construct the bind here and
	// start the relay's Run loop after the tunnel is up.
	var dBind *derp.DerpBind
	var derpClient *derp.Client
	var tunnelOpts []tunnel.Option
	if cfg.DERPRelayURL != "" {
		dBind, derpClient = buildDERPBind(cfg)
		if dBind != nil {
			tunnelOpts = append(tunnelOpts, tunnel.WithBind(dBind))
		}
	}

	// Start WG device with no mesh IP yet; SetMeshIP is called from OnWelcome.
	tun, err := tunnel.New(privKeyBase64, "", tunnelOpts...)
	if err != nil {
		slog.Error("tunnel init failed",
			"err", err,
			"hint_linux", "ensure CAP_NET_ADMIN is set: sudo setcap cap_net_admin+ep ./potluck-mesh",
			"hint_mac", "utun creation needs root in dev — run with sudo, or sign the binary with the Network Extension entitlement",
			"hint_windows", "wintun.dll must be installed; run the installer once as admin",
		)
		return 1
	}
	defer tun.Close()

	// ---- peer state cache ---- //
	ps := peerstate.New()

	// ---- event bus ---- //
	bus := events.NewBus()
	defer bus.Close()

	// ---- IPC state ---- //
	ipcState := localapi.NewState()
	// Pre-populate the WG public key from config so /status can report it
	// even before the first welcome arrives.
	if cfg.WGPublicKey != "" {
		ipcState.SetWGPublicKey(cfg.WGPublicKey)
	}

	// ---- STUN resolver ---- //
	resolver := stun.NewResolver(cfg.STUNServers)

	// ---- signaling client ---- //
	handler := newRosterHandler(tun, ps, bus, ipcState)

	// Wire DERP-routing introspection so the roster + punch_coord handlers
	// know not to overwrite the synthetic derp:<pubkey> endpoint with a
	// raw IP:port when a peer is currently being routed via the relay.
	// nil-safe when --derp-url isn't set: dBind stays nil and the gate
	// returns false (always allow direct updates).
	if dBind != nil {
		handler.setIsDERPRoutedFn(func(pubkey string) bool {
			kb, err := base64.StdEncoding.DecodeString(pubkey)
			if err != nil || len(kb) != 32 {
				return false
			}
			var pub [32]byte
			copy(pub[:], kb)
			return dBind.IsPeerDERPRouted(pub)
		})

		// Proactive flip-to-DERP on roster receive. Same decoding pattern as
		// above; reuses the existing flipToDERP helper. Errors are logged
		// inside flipToDERP, so this closure stays terse.
		handler.setFlipPeerToDERPFn(func(pubkey string) {
			kb, err := base64.StdEncoding.DecodeString(pubkey)
			if err != nil || len(kb) != 32 {
				slog.Warn("flip-on-roster: bad pubkey", "pubkey_prefix", util.TruncatePubkey(pubkey))
				return
			}
			var pub [32]byte
			copy(pub[:], kb)
			flipToDERP(tun, dBind, pubkey, pub)
		})
	}

	client := msignal.NewClient(cfg.SignalURL(), cfg.WGPublicKey, handler)

	// Give the handler a reference to the client so it can call
	// SendRequestPunch from OnWelcome / OnRosterUpdate.
	handler.setClient(client)

	// signalRestart is called from POST /pair after enrollment to reconnect
	// the signal client with the new WG key. The signal client Run loop
	// reconnects automatically when its connection is dropped; we trigger a
	// fresh connection by sending a no-op endpoint update, which causes the
	// write loop to flush and the coordinator to process the new key on the
	// next hello after the existing connection closes naturally.
	//
	// For v0 this is best-effort. The signal client will reconnect within its
	// normal backoff cycle even without an explicit trigger.
	signalRestart := func() {
		slog.Info("localapi: pair: signal client reconnect triggered")
		// Send a no-op endpoint update to prod the existing connection. If the
		// new key differs, the coordinator will close with node_not_enrolled and
		// the client reconnects; on the next hello it uses cfg.WGPublicKey which
		// was updated by the /pair handler via ipcState.SetWGPublicKey.
		client.SendEndpointUpdate(ipcState.MeshIP())
	}

	// ---- coordinator API client ---- //
	coordClient := coordapi.New(cfg.CoordinatorURL)

	// ---- local IPC server ---- //
	ipcSrv := localapi.New(localapi.Config{
		Bus:         bus,
		PeerStore:   ps,
		State:       ipcState,
		PeerReachFn: tun.PeerReachability,
		IsDERPRoutedFn: func(pubkey string) bool {
			if dBind == nil {
				return false
			}
			kb, err := base64.StdEncoding.DecodeString(pubkey)
			if err != nil || len(kb) != 32 {
				return false
			}
			var pub [32]byte
			copy(pub[:], kb)
			return dBind.IsPeerDERPRouted(pub)
		},
		IsPeerConfiguredFn: func(pubkey string) bool {
			ok, _ := tun.IsPeerConfigured(pubkey)
			return ok
		},
		CoordClient:   coordClient,
		SignalRestart: signalRestart,
		WGPrivKeyPath: cfg.WGPrivateKeyPath,
		SocketPath:    cfg.LocalAPISocketPath,
		Platform:      cfg.Platform,
	})

	// Publish endpoint changes to the bus from STUN.
	onEndpointChange := func(ep string) {
		ipcState.SetExternalEndpoint(ep)
		bus.Publish(events.NewEndpointChanged(ep))
		client.SendEndpointUpdate(ep)
	}

	// ---- STUN goroutine ---- //
	go runSTUNLoop(ctx, resolver, onEndpointChange, cfg.STUNRefreshInterval)

	// ---- reachability + DERP-fallback goroutine ---- //
	go runReachabilityLoop(ctx, tun, ps, dBind, bus)

	// ---- DERP relay Run loop (optional) ---- //
	if derpClient != nil {
		go func() {
			if err := derpClient.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
				slog.Error("derp: client exited with error", "err", err)
			}
		}()
	}

	// ---- local IPC server goroutine ---- //
	// The IPC server runs concurrently; on shutdown we cancel its context
	// first so WS clients receive a clean "status_changed: stopped" event
	// before we tear down the signal client and tunnel.
	ipcCtx, ipcCancel := context.WithCancel(ctx)
	ipcDone := make(chan struct{})
	go func() {
		defer close(ipcDone)
		if err := ipcSrv.Serve(ipcCtx); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("localapi: IPC server exited with error", "err", err)
		}
	}()

	// ---- signaling client (blocks until ctx is cancelled) ---- //
	slog.Info("connecting to coordinator signal channel", "url", cfg.SignalURL())
	if err := client.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("signal client exited with error", "err", err)
		ipcCancel()
		<-ipcDone
		return 1
	}

	// Graceful shutdown sequence:
	//  1. Publish stopped event + close IPC server (WS clients get clean disconnect).
	//  2. Close event bus (drains remaining subscribers).
	//  3. Tunnel closed by defer above.
	ipcCancel()
	<-ipcDone

	slog.Info("potluck-mesh shutdown complete")
	return 0
}

// runSTUNLoop runs an initial STUN binding request then refreshes on interval.
// On each successful resolve, if the endpoint changed, it calls onEndpointChange.
// Errors are logged and retried; they never crash.
//
// The initial resolve uses a short exponential backoff so that startup isn't
// blocked if the STUN server is temporarily unreachable.
func runSTUNLoop(
	ctx context.Context,
	resolver *stun.Resolver,
	onEndpointChange func(ep string),
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
		onEndpointChange(ep)
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
			onEndpointChange(ep)
		}
	}
}

// derpFallbackThreshold is the age of the last WG handshake after which we
// consider a peer unreachable via direct UDP and flip it to DERP routing.
// Chosen to be longer than handshakeFreshWindow (3 min) so that a single
// missed keepalive doesn't immediately trigger DERP fallback.
const derpFallbackThreshold = 30 * time.Second

// runReachabilityLoop polls the WG UAPI every 30s for each known peer's
// last-handshake time. It logs a message only on state transitions
// (unreachable→reachable or vice versa) and once per peer on startup.
//
// When dBind is non-nil (DERP relay is available), peers that have had no
// fresh handshake for derpFallbackThreshold are flipped to DERP routing via
// dBind.SetPeerRouting. When a peer recovers (fresh handshake observed), it is
// flipped back to direct UDP.
//
// A peer is considered reachable when its last handshake is within
// handshakeFreshWindow (3 minutes). WireGuard renegotiates every ~3 minutes
// when traffic is flowing; a missed renegotiation means the peer is gone.
//
// Privacy: PeerReachability parses UAPI output internally and does not log
// raw IpcGet output (which would include the device's own private key).
func runReachabilityLoop(ctx context.Context, tun *tunnel.Tunnel, ps *peerstate.Store, dBind *derp.DerpBind, bus *events.Bus) {
	// reachableState maps pubkey → last known reachable bool.
	// true = reachable, false = unreachable.
	// Peers not yet seen are treated as "unknown" (logged once as initial state).
	type peerReachState struct {
		reachable  bool
		seenOnce   bool
		derpRouted bool // true if we've flipped this peer to DERP routing
	}
	states := make(map[string]*peerReachState)

	check := func() {
		for _, p := range ps.All() {
			pubkey := p.WGPublicKey // base64
			lastHS, _, _, err := tun.PeerReachability(pubkey)
			if err != nil {
				slog.Debug("reachability: query error",
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"err", err,
				)
				continue
			}

			nowReachable := !lastHS.IsZero() && time.Since(lastHS) < handshakeFreshWindow
			handshakeStale := lastHS.IsZero() || time.Since(lastHS) >= derpFallbackThreshold

			st, known := states[pubkey]
			if !known {
				st = &peerReachState{}
				states[pubkey] = st
			}

			// Decode pubkey bytes once — needed for DERP flip operations.
			// Only decode when dBind is active to avoid work when DERP is off.
			var pubkeyBytes [32]byte
			peerDerpBind := dBind // local copy; set nil on decode failure for this peer only
			if peerDerpBind != nil {
				kb, decErr := base64.StdEncoding.DecodeString(pubkey)
				if decErr != nil || len(kb) != 32 {
					slog.Warn("reachability: could not decode peer pubkey, DERP flip skipped",
						"pubkey_prefix", util.TruncatePubkey(pubkey),
					)
					peerDerpBind = nil
				} else {
					copy(pubkeyBytes[:], kb)
				}
			}

			if !st.seenOnce {
				// Log initial state once per peer regardless of value.
				st.reachable = nowReachable
				st.seenOnce = true
				slog.Info("reachability: initial state",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"reachable", nowReachable,
					"last_handshake_ago", formatHandshakeAge(lastHS),
				)
				// On first observation, if the handshake is already stale and DERP
				// is available, flip immediately.
				if peerDerpBind != nil && handshakeStale && !st.derpRouted {
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
					st.derpRouted = true
				}
				continue
			}

			if nowReachable == st.reachable {
				// No reachability state transition — but still check DERP fallback
				// in case handshake just crossed the threshold.
				if peerDerpBind != nil && !nowReachable && handshakeStale && !st.derpRouted {
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
					st.derpRouted = true
				}
				continue
			}

			// State transition.
			st.reachable = nowReachable

			// Build a PeerSnapshot for event publishing (mesh-layer data only).
			peerSnap := rosterPeerToSnapshot(p, nowReachable, st.derpRouted && nowReachable)

			if nowReachable {
				slog.Info("reachability: peer became reachable",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"last_handshake_ago", formatHandshakeAge(lastHS),
				)
				// Publish peer_updated with reachable=true.
				if bus != nil {
					bus.Publish(events.NewPeerUpdated(peerSnap))
				}
				// DO NOT auto-flip back to direct UDP here.
				//
				// The "peer became reachable" event fires when there's a fresh
				// handshake — but the handshake itself may have completed VIA
				// DERP, not via direct UDP. Flipping back to direct would set
				// the WG peer endpoint to a raw IP:port that the NAT can't
				// actually deliver to (e.g. hairpin, CGNAT), silently breaking
				// the connection we just established. Stay on DERP; the cost
				// is one relay hop of latency, which is negligible vs. the
				// alternative ("handshake completes then everything stops
				// working").
				//
				// Future enhancement: track whether the most recent handshake
				// arrived via direct UDP (TX/RX counters on the inner bind vs.
				// the DERP-inject path) and flip back only when there's
				// evidence direct UDP actually works for this peer.
				_ = peerDerpBind // intentionally not flipping back
			} else {
				slog.Warn("reachability: peer became unreachable",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"last_handshake_ago", formatHandshakeAge(lastHS),
				)
				// Publish peer_updated with reachable=false.
				if bus != nil {
					bus.Publish(events.NewPeerUpdated(peerSnap))
				}
				// Peer lost: flip to DERP if available and handshake is stale.
				if peerDerpBind != nil && handshakeStale && !st.derpRouted {
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
					st.derpRouted = true
				}
			}
		}

		// Prune states for peers that have been removed from the roster.
		allNodePubkeys := make(map[string]bool)
		for _, p := range ps.All() {
			allNodePubkeys[p.WGPublicKey] = true
		}
		for k := range states {
			if !allNodePubkeys[k] {
				delete(states, k)
			}
		}
	}

	// Run once immediately so startup logs show initial reachability.
	check()

	ticker := time.NewTicker(reachabilityCheckInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			check()
		}
	}
}

// flipToDERP marks the peer as DERP-routed in the bind and updates the WG
// peer endpoint to the synthetic address so wg-go routes outbound packets via
// DerpBind.Send (which dispatches to the relay).
func flipToDERP(tun *tunnel.Tunnel, dBind *derp.DerpBind, pubkeyBase64 string, pubkeyBytes [32]byte) {
	syntheticEp := derp.SyntheticEndpointString(pubkeyBytes)
	dBind.SetPeerRouting(pubkeyBytes, true)
	if err := tun.UpdatePeerEndpoint(pubkeyBase64, syntheticEp); err != nil {
		slog.Error("derp_fallback: UpdatePeerEndpoint to synthetic failed",
			"pubkey_prefix", util.TruncatePubkey(pubkeyBase64),
			"err", err,
		)
		// Roll back the routing change to avoid a black-hole.
		dBind.SetPeerRouting(pubkeyBytes, false)
		return
	}
	slog.Info("derp_fallback: peer flipped to DERP routing",
		"pubkey_prefix", util.TruncatePubkey(pubkeyBase64),
		"synthetic_ep", syntheticEp,
	)
}

// flipToDirectUDP reverts the peer back to direct UDP routing. The real
// endpoint must come from the latest STUN/roster data; we clear the routing
// flag and update the endpoint to empty (WG keeps the peer configured but will
// wait for an inbound initiation or a fresh endpoint_update from the coordinator).
func flipToDirectUDP(tun *tunnel.Tunnel, dBind *derp.DerpBind, pubkeyBase64 string, pubkeyBytes [32]byte) {
	dBind.SetPeerRouting(pubkeyBytes, false)
	// Set endpoint to empty string — tunnel.UpdatePeerEndpoint is a no-op for
	// empty strings (see its implementation), so the last-known real endpoint
	// stays configured in wg-go. The coordinator will deliver a fresh
	// endpoint_update when the peer's STUN binding is current.
	slog.Info("derp_fallback: peer reverted to direct UDP",
		"pubkey_prefix", util.TruncatePubkey(pubkeyBase64),
	)
}

// formatHandshakeAge returns a human-readable age string for a last-handshake
// time. Returns "<never>" for the zero value.
func formatHandshakeAge(t time.Time) string {
	if t.IsZero() {
		return "<never>"
	}
	return time.Since(t).Round(time.Second).String()
}

// punchEntry records when we last issued a request_punch for a given peer.
type punchEntry struct {
	lastRequested time.Time
}

// rosterHandler receives welcome + roster update + punch coord events from the
// signal client and applies them to the WireGuard tunnel.
type rosterHandler struct {
	tun      *tunnel.Tunnel
	ps       *peerstate.Store
	client   *msignal.Client // set after construction via setClient
	bus      *events.Bus
	ipcState *localapi.State

	// isDERPRoutedFn reports whether a given peer (by base64 pubkey) is
	// currently being routed via the DERP relay. When true, signal-driven
	// endpoint updates (roster_update.Updated, punch_coord) MUST NOT overwrite
	// the synthetic DERP endpoint on the WG device — doing so silently
	// breaks the relay path. We still update the peerstate cache so that
	// when the reachability loop flips the peer back to direct, it has the
	// latest known STUN endpoint to point at.
	isDERPRoutedFn func(pubkey string) bool

	// flipPeerToDERPFn proactively switches a peer to DERP routing at the
	// moment we first see it. Called from OnWelcome / OnRosterUpdate so
	// peers DON'T burn the first 30 seconds of life trying direct-UDP
	// handshakes that can't succeed under hairpin/symmetric NAT. The
	// reachability loop still handles re-flipping on later state changes.
	flipPeerToDERPFn func(pubkey string)

	// punchMu guards punchState.
	punchMu    sync.Mutex
	punchState map[string]*punchEntry // keyed by node_id
}

func newRosterHandler(tun *tunnel.Tunnel, ps *peerstate.Store, bus *events.Bus, ipcState *localapi.State) *rosterHandler {
	return &rosterHandler{
		tun:        tun,
		ps:         ps,
		bus:        bus,
		ipcState:   ipcState,
		punchState: make(map[string]*punchEntry),
	}
}

// setIsDERPRoutedFn wires the DERP-routing introspection callback. Called in
// main after the DerpBind is constructed. nil-safe — when DERP is disabled
// (no --derp-url) this stays nil and the handler degrades to its prior
// always-overwrite behavior.
func (h *rosterHandler) setIsDERPRoutedFn(fn func(pubkey string) bool) {
	h.isDERPRoutedFn = fn
}

// setFlipPeerToDERPFn wires the proactive-flip callback. Same nil-safety
// story as setIsDERPRoutedFn — left nil when DERP is disabled.
func (h *rosterHandler) setFlipPeerToDERPFn(fn func(pubkey string)) {
	h.flipPeerToDERPFn = fn
}

// setClient wires the signal client back into the handler after construction.
// Called in main before client.Run so there's no data race: the client goroutine
// hasn't started yet.
func (h *rosterHandler) setClient(c *msignal.Client) {
	h.client = c
}

// OnWelcome is called once per signaling connection after a successful hello.
// It:
//  1. Assigns the mesh IP to the TUN interface.
//  2. Populates the peer state cache from the full roster.
//  3. Calls SetPeers to atomically configure all WG peers.
//  4. Sends request_punch for each peer that has a STUN endpoint.
func (h *rosterHandler) OnWelcome(w msignal.Welcome) {
	slog.Info("mesh welcome received",
		"node_id", w.NodeID,
		"mesh_ip", w.MeshIP,
		"household_id", w.HouseholdID,
		"roster_size", len(w.Roster),
	)

	// 1. Update IPC state so /status reflects the welcome.
	if h.ipcState != nil {
		h.ipcState.SetWelcome(w.NodeID, w.HouseholdID, w.MeshIP, h.ipcState.WGPublicKey())
	}

	// 2. Assign mesh IP.
	if w.MeshIP != "" {
		if err := h.tun.SetMeshIP(w.MeshIP); err != nil {
			slog.Error("tunnel: SetMeshIP failed", "mesh_ip", w.MeshIP, "err", err)
			// Non-fatal: WG peers can still be configured even if the
			// interface address assignment fails (e.g. missing ip/ifconfig).
		}
	}

	// 3. Populate peer cache from the roster (excluding ourselves).
	h.ps.Reset(w.Roster)

	// 4. Build WG Peer list and apply.
	peers := rosterToPeers(w.Roster)
	if err := h.tun.SetPeers(peers); err != nil {
		slog.Error("tunnel: SetPeers failed on welcome", "err", err)
		return
	}

	// 4b. Install host routes for every peer so the kernel actually delivers
	// packets to the WG interface. Without these, only the local mesh IP /32
	// is in the routing table and packets to peer mesh IPs fall through to
	// the default gateway (and the relay never sees a byte).
	for _, p := range w.Roster {
		if p.MeshIP == "" {
			continue
		}
		if err := h.tun.AddPeerRoute(p.MeshIP); err != nil {
			slog.Warn("tunnel: AddPeerRoute failed on welcome",
				"node_id", p.NodeID,
				"mesh_ip", p.MeshIP,
				"err", err,
			)
		}
	}

	// 4c. Proactively flip every roster peer to DERP routing immediately,
	// rather than waiting 30s for the reachability loop's first tick. The
	// 30s gap was burning handshakes via direct UDP (which can't succeed
	// under hairpin/symmetric NAT anyway) and leaving the household toggle
	// greyed out during that window. DERP is the safe default; the loop
	// still detects later transitions if direct ever works.
	if h.flipPeerToDERPFn != nil {
		for _, p := range w.Roster {
			h.flipPeerToDERPFn(p.WGPublicKey)
		}
	}

	// 5. Publish status_changed + peer_added events for each roster peer.
	if h.bus != nil {
		h.bus.Publish(events.NewStatusChanged(string(localapi.StatusRunning)))
		for _, peer := range w.Roster {
			h.bus.Publish(events.NewPeerAdded(rosterPeerToSnapshot(peer, false, false)))
		}
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

	// 6. Send request_punch for each peer with a known endpoint.
	for _, peer := range w.Roster {
		if peer.LastEndpoint != nil && *peer.LastEndpoint != "" {
			h.maybeSendRequestPunch(peer.NodeID)
		}
	}
}

// OnRosterUpdate applies incremental peer changes to the WireGuard tunnel and
// the in-memory peer cache. Also sends request_punch for peers with endpoints.
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
		// Install the kernel route for the new peer's mesh IP.
		if p.MeshIP != "" {
			if err := h.tun.AddPeerRoute(p.MeshIP); err != nil {
				slog.Warn("tunnel: AddPeerRoute failed for new peer",
					"node_id", p.NodeID,
					"mesh_ip", p.MeshIP,
					"err", err,
				)
			}
		}
		// Proactively flip to DERP for the new peer (same rationale as
		// OnWelcome — don't waste 30s on direct-UDP handshakes that
		// can't succeed under hairpin/symmetric NAT).
		if h.flipPeerToDERPFn != nil {
			h.flipPeerToDERPFn(p.WGPublicKey)
		}
		if h.bus != nil {
			h.bus.Publish(events.NewPeerAdded(rosterPeerToSnapshot(p, false, false)))
		}
		// Newly added peer: punch if endpoint known.
		if p.LastEndpoint != nil && *p.LastEndpoint != "" {
			h.maybeSendRequestPunch(p.NodeID)
		}
	}

	// Updated peers: update the endpoint (and cache) if endpoint changed.
	for _, p := range u.Updated {
		h.ps.Upsert(p)
		if p.LastEndpoint != nil && *p.LastEndpoint != "" {
			// If the peer is currently DERP-routed, do NOT push the raw
			// IP:port back onto the WG device — that would silently break
			// the relay path. The peerstate cache still records the latest
			// endpoint (Upsert above) so the reachability loop can flip
			// back to direct later when handshakes resume.
			if h.isDERPRoutedFn != nil && h.isDERPRoutedFn(p.WGPublicKey) {
				slog.Debug("roster update: peer is DERP-routed, skipping WG endpoint overwrite",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(p.WGPublicKey),
					"would_have_set", *p.LastEndpoint,
				)
			} else if err := h.tun.UpdatePeerEndpoint(p.WGPublicKey, *p.LastEndpoint); err != nil {
				slog.Error("tunnel: UpdatePeerEndpoint failed",
					"node_id", p.NodeID,
					"err", err,
				)
			}
			if h.bus != nil {
				h.bus.Publish(events.NewPeerUpdated(rosterPeerToSnapshot(p, false, false)))
			}
			// Updated endpoint: fresh punch regardless of handshake age.
			h.maybeSendRequestPunch(p.NodeID)
		} else {
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
		// Tear down the host route so packets don't black-hole at this iface.
		if cached.MeshIP != "" {
			if err := h.tun.RemovePeerRoute(cached.MeshIP); err != nil {
				slog.Warn("tunnel: RemovePeerRoute failed",
					"node_id", nodeID,
					"mesh_ip", cached.MeshIP,
					"err", err,
				)
			}
		}
		if h.bus != nil {
			h.bus.Publish(events.NewPeerRemoved(nodeID))
		}
		// Clean up punch throttle state for removed peer.
		h.punchMu.Lock()
		delete(h.punchState, nodeID)
		h.punchMu.Unlock()
	}
}

// OnPunchCoord is called when the coordinator delivers a punch_coord message
// to this node. Both the initiator and the responder call UpdatePeerEndpoint
// so WireGuard's handshake-init fires from both sides concurrently, punching
// through both NAT mappings simultaneously.
//
// If peer_endpoint is nil, the peer has not STUN'd yet — log a warning and
// skip; we'll get another punch_coord when the peer's endpoint is known.
func (h *rosterHandler) OnPunchCoord(coord msignal.PunchCoord) {
	if coord.PeerEndpoint == nil || *coord.PeerEndpoint == "" {
		slog.Warn("punch_coord: peer has no endpoint yet, skipping",
			"peer_node_id", coord.PeerNodeID,
			"role", coord.Role,
		)
		return
	}

	slog.Info("punch_coord received",
		"peer_node_id", coord.PeerNodeID,
		"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
		"role", coord.Role,
		"endpoint", *coord.PeerEndpoint,
	)

	// If the peer is currently DERP-routed, skip the WG endpoint overwrite.
	// Otherwise this punch_coord would replace the synthetic derp:<pubkey>
	// endpoint with a raw IP:port that the NAT can't actually deliver to.
	// We still let the coordinator's punch broker do its thing on the
	// other side — if both sides recover direct reachability, the next
	// successful handshake will trigger the reachability loop to flip the
	// peer back to direct routing.
	if h.isDERPRoutedFn != nil && h.isDERPRoutedFn(coord.PeerWGPublicKey) {
		slog.Debug("punch_coord: peer is DERP-routed, skipping WG endpoint overwrite",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
			"would_have_set", *coord.PeerEndpoint,
		)
		return
	}

	// Configure the peer's endpoint. This causes wg-go to fire a
	// handshake-init toward the peer's NAT-mapped address, creating our
	// side of the hole. The peer does the same simultaneously (they also
	// received a punch_coord from the coordinator), creating their side.
	// When both handshake-inits cross in flight, the NATs on both sides
	// record the mapping and the next inbound packet traverses cleanly.
	if err := h.tun.UpdatePeerEndpoint(coord.PeerWGPublicKey, *coord.PeerEndpoint); err != nil {
		slog.Error("tunnel: UpdatePeerEndpoint failed in punch_coord handler",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
			"err", err,
		)
	}
}

// maybeSendRequestPunch sends request_punch for the given peer if the peer
// doesn't have a fresh WG handshake and we haven't requested a punch for this
// peer within the last punchThrottlePeriod. Concurrency-safe.
func (h *rosterHandler) maybeSendRequestPunch(nodeID string) {
	if h.client == nil {
		return
	}

	// Check WG handshake freshness before doing anything.
	// If we already have a live session with this peer, there's no need to
	// re-punch. Look up the public key from the peer cache.
	cached, ok := h.ps.Get(nodeID)
	if !ok {
		// Peer not in cache — shouldn't happen since callers upsert first,
		// but be defensive.
		slog.Debug("maybeSendRequestPunch: peer not in cache", "node_id", nodeID)
		return
	}

	lastHS, _, _, err := h.tun.PeerReachability(cached.WGPublicKey)
	if err == nil && !lastHS.IsZero() && time.Since(lastHS) < handshakeFreshWindow {
		slog.Debug("punch: peer has fresh handshake, skipping request_punch",
			"node_id", nodeID,
			"pubkey_prefix", util.TruncatePubkey(cached.WGPublicKey),
			"handshake_age", time.Since(lastHS).Round(time.Second),
		)
		return
	}

	// Throttle: at most one request_punch per peer per punchThrottlePeriod.
	h.punchMu.Lock()
	entry, exists := h.punchState[nodeID]
	if !exists {
		entry = &punchEntry{}
		h.punchState[nodeID] = entry
	}
	if exists && time.Since(entry.lastRequested) < punchThrottlePeriod {
		h.punchMu.Unlock()
		slog.Debug("punch: throttled, skipping request_punch",
			"node_id", nodeID,
			"next_allowed_in", (punchThrottlePeriod - time.Since(entry.lastRequested)).Round(time.Second),
		)
		return
	}
	entry.lastRequested = time.Now()
	h.punchMu.Unlock()

	slog.Info("punch: sending request_punch",
		"node_id", nodeID,
		"pubkey_prefix", util.TruncatePubkey(cached.WGPublicKey),
	)
	h.client.SendRequestPunch(nodeID)
}

// rosterPeerToSnapshot converts a RosterPeer to an events.PeerSnapshot for
// publishing over the event bus. Only mesh-layer metadata is included.
func rosterPeerToSnapshot(p msignal.RosterPeer, reachable bool, derpRouted bool) events.PeerSnapshot {
	snap := events.PeerSnapshot{
		NodeID:      p.NodeID,
		NodeName:    p.NodeName,
		MeshIP:      p.MeshIP,
		WGPublicKey: util.TruncatePubkey(p.WGPublicKey), // truncated for privacy in event stream
		Reachable:   reachable,
	}
	if p.LastEndpoint != nil {
		snap.LastEndpoint = p.LastEndpoint
	}
	if p.Platform != nil {
		snap.Platform = p.Platform
	}
	if reachable {
		var mode string
		if derpRouted {
			mode = "derp"
		} else {
			mode = "direct"
		}
		snap.ReachabilityMode = &mode
	}
	return snap
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
//
// PersistentKeepalive is 25s (matches Tailscale's default). This is REQUIRED
// for DERP-routed peers — without keepalive, idle WG sessions age out after
// ~3 min and the household toggle flickers off in the UI. The cost is ~150
// bytes every 25s per peer, which is negligible for household-scope traffic
// over a relay you control. The original "keepalive=0 by design" stance was
// written before DERP fallback existed; revisit if/when direct-UDP-only
// scenarios become important.
func rosterPeerToTunnelPeer(p msignal.RosterPeer) tunnel.Peer {
	ep := ""
	if p.LastEndpoint != nil {
		ep = *p.LastEndpoint
	}
	return tunnel.Peer{
		PublicKey:           p.WGPublicKey,
		AllowedIPs:          []string{p.MeshIP + "/32"},
		Endpoint:            ep,
		PersistentKeepalive: 25,
	}
}

// ---------------------------------------------------------------------------
// DERP helpers
// ---------------------------------------------------------------------------

// buildDERPBind constructs a DerpBind and its underlying Client for use when
// POTLUCK_DERP_URL is configured. Returns (nil, nil) on any setup error so the
// caller can fall back to plain UDP.
//
// The returned DerpBind should be installed via tunnel.WithBind before
// tunnel.New is called. The returned Client's Run method should be started in
// a goroutine after the tunnel is up.
func buildDERPBind(cfg *config.Config) (*derp.DerpBind, *derp.Client) {
	slog.Info("derp: relay URL configured, constructing DerpBind",
		"url", cfg.DERPRelayURL,
	)

	keyBytes, err := base64.StdEncoding.DecodeString(cfg.WGPublicKey)
	if err != nil || len(keyBytes) != 32 {
		slog.Warn("derp: could not decode WG public key; DERP disabled",
			"pubkey_prefix", util.TruncatePubkey(cfg.WGPublicKey),
		)
		return nil, nil
	}
	var ourRawPubkey [32]byte
	copy(ourRawPubkey[:], keyBytes)

	// We create the DerpBind first with a nil onPacketRx placeholder,
	// then create the Client with the bind's Inject method as the handler.
	// This breaks the construction cycle: bind needs the client reference
	// for Send; client needs the bind's Inject for Rx.
	//
	// We construct them in two steps and use a closure in the rxHandler that
	// captures the bind pointer by pointer so it's populated by the time
	// the relay delivers its first packet.
	var bindPtr *derp.DerpBind

	rxHandler := func(src [32]byte, payload []byte) {
		b := bindPtr
		if b == nil {
			slog.Warn("derp: received packet before bind initialised, dropping",
				"src_prefix", util.TruncatePubkeyBytes(src),
			)
			return
		}
		b.Inject(src, payload)
	}

	derpClient := derp.New(
		cfg.DERPRelayURL,
		makeDERPJWTFetcher(cfg),
		ourRawPubkey,
		rxHandler,
	)

	inner := conn.NewDefaultBind()
	bind := derp.NewDerpBind(inner, derpClient)
	bindPtr = bind // wire the closure

	return bind, derpClient
}

// derpTokenResponse is the JSON body returned by POST /mesh/derp-token.
type derpTokenResponse struct {
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
	Region    string `json:"region"`
}

// makeDERPJWTFetcher returns a JWTFetcher that calls the coordinator's
// POST /mesh/derp-token endpoint to get a fresh auth token.
func makeDERPJWTFetcher(cfg *config.Config) derp.JWTFetcher {
	coordinatorURL := cfg.CoordinatorURL
	wgPublicKey := cfg.WGPublicKey

	return func(ctx context.Context) (string, error) {
		body := map[string]string{
			"wg_public_key": wgPublicKey,
			"region":        "ord", // v0: single region
		}
		bodyBytes, _ := json.Marshal(body)

		req, err := http.NewRequestWithContext(
			ctx,
			http.MethodPost,
			coordinatorURL+"/mesh/derp-token",
			bytes.NewReader(bodyBytes),
		)
		if err != nil {
			return "", fmt.Errorf("build derp-token request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			return "", fmt.Errorf("fetch derp-token: %w", err)
		}
		defer resp.Body.Close()

		if resp.StatusCode != http.StatusOK {
			return "", fmt.Errorf("derp-token endpoint returned HTTP %d", resp.StatusCode)
		}

		var result derpTokenResponse
		if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
			return "", fmt.Errorf("decode derp-token response: %w", err)
		}
		if result.Token == "" {
			return "", fmt.Errorf("derp-token response had empty token")
		}

		slog.Debug("derp: JWT fetched from coordinator",
			"region", result.Region,
			"expires_at", result.ExpiresAt,
		)
		return result.Token, nil
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
