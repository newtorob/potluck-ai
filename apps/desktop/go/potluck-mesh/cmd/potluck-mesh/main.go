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
//     handshakeFreshWindow (3min), outbound packets to that peer are sent
//     via the relay instead of (or in addition to) direct UDP.
//   - JWT fetcher: hits the coordinator's POST /mesh/derp-token endpoint
//     to obtain a short-lived HMAC-signed token for DERP auth.
//
// Week 5b additions (DERP last-mile):
//   - DerpBind (internal/derp/bind.go): custom conn.Bind that intercepts
//     wg-go's send/receive paths. Inbound DERP packets are injected via
//     Inject(); outbound to syntheticEndpoints are dispatched to the relay.
//   - Automatic DERP fallback: runReachabilityLoop flips unreachable peers
//     (no fresh handshake within handshakeFreshWindow) to DERP routing via
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
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/conn"

	"github.com/potluck-ai/potluck-mesh/internal/config"
	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/lanpair"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/localep"
	"github.com/potluck-ai/potluck-mesh/internal/nodetoken"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/runtimesettings"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/signingkey"
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

	// ---- runtime coordinator-URL override (internal/runtimesettings) ---- //
	// A prior PATCH /settings call (internal/localapi's handleSettingsPatch)
	// may have persisted a saved coordinator URL next to the WG private key.
	// This is the ONLY way to point a static-plist/unit LaunchDaemon/systemd/
	// Windows-service build (baked-in prod coordinator, no flag to change
	// post-install) at a self-hosted or LAN coordinator, so a saved override
	// always wins over the --coordinator-url flag/env default — loaded here,
	// before ANYTHING coordinator-facing (coordClient, the DERP bind, the
	// signal client) is constructed below, and before the startup log line
	// so it reflects the URL actually in use.
	settingsPath := runtimesettings.Path(cfg.WGPrivateKeyPath)
	persistedSettings, err := runtimesettings.Load(settingsPath)
	if err != nil {
		slog.Error("runtime settings load failed", "err", err)
		return 1
	}
	if persistedSettings.CoordinatorURL != "" {
		slog.Info("runtime settings: applying persisted coordinator_url override",
			"coordinator_url", persistedSettings.CoordinatorURL,
		)
		cfg.CoordinatorURL = persistedSettings.CoordinatorURL
	}

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

	// ---- signing keypair (DERP-token proof-of-possession; internal/signingkey) ---- //
	// Generated unconditionally here (mirrors the WG keypair above) so it
	// exists before the first /pair call even when POTLUCK_DERP_URL is unset
	// this run. DISTINCT from the WG (Curve25519) key — Ed25519, sign-only.
	// This does NOT register anything with the coordinator by itself; that
	// only happens via a successful POST /mesh/enroll (see
	// internal/localapi's handlePair) and is tracked locally by
	// internal/signingkey's registration marker.
	if _, err := signingkey.LoadOrGenerate(cfg.SigningPrivateKeyPath); err != nil {
		slog.Error("signing keypair init failed", "err", err)
		return 1
	}

	// ---- node token (per-node bearer credential; internal/nodetoken) ---- //
	// Sited next to the WG private key. A missing file is normal (unpaired,
	// or enrolled before this feature shipped) — Load returns "" with no
	// error in that case, and every coordinator call below just falls back
	// to the legacy coordinator-token credential (or dev passthrough).
	nodeTokenPath := nodetoken.Path(cfg.WGPrivateKeyPath)
	loadedNodeToken, err := nodetoken.Load(nodeTokenPath)
	if err != nil {
		slog.Error("node token load failed", "err", err)
		return 1
	}

	// ---- coordinator API client ---- //
	// Constructed early (before the DERP bind + signal client below) because
	// both of those read the LIVE node token via coordClient.NodeToken as a
	// function value — this Client is the single shared source of truth for
	// the bearer credential. A fresh token minted by a later /pair
	// (internal/localapi's handlePair calls coordClient.SetNodeToken) is
	// picked up automatically by every caller on their next request/reconnect
	// with no separate propagation plumbing needed.
	coordClient := coordapi.New(cfg.CoordinatorURL)
	coordClient.CoordinatorToken = cfg.CoordinatorToken
	coordClient.SetNodeToken(loadedNodeToken)

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
		dBind, derpClient = buildDERPBind(cfg, coordClient.BaseURL, coordClient.NodeToken)
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
	// The coordinator URL is a static launch-flag value; expose it in /status
	// so the frontend can verify it matches what it expects (drift prevention).
	ipcState.SetCoordinatorURL(cfg.CoordinatorURL)
	// Pre-populate the WG public key from config so /status can report it
	// even before the first welcome arrives.
	if cfg.WGPublicKey != "" {
		ipcState.SetWGPublicKey(cfg.WGPublicKey)
	}
	// Record the WG transport port so the /status readiness probe can verify
	// the UDP socket is actually bound (wg_port_listening field).
	ipcState.SetWGPort(tun.WGPort())

	// ---- STUN resolver ---- //
	// localPort is pinned to the WG transport port (tun.WGPort(), 51820 by
	// default) rather than an OS-assigned ephemeral one. This is bug #3 of the
	// direct-connectivity fix: on a per-flow/per-socket NAT mapping (the
	// common case for consumer + CGNAT routers), the external ip:port a
	// throwaway ephemeral-port STUN probe observes has NO relation to the
	// mapping the WG data plane actually uses — so even a perfect hole-punch
	// would target the wrong port. Binding the probe to the SAME local port
	// observes the SAME NAT mapping WG traffic uses (for endpoint-independent
	// / cone NATs — the majority of home routers — the mapping is keyed by
	// local port only, so this holds regardless of destination). See
	// internal/stun's bindProbeConn for the same-port bind + graceful
	// ephemeral-port fallback when the WG socket already owns the port.
	resolver := stun.NewResolver(cfg.STUNServers, tun.WGPort())

	// ---- per-peer routing-mode tracker (Phase 2) ---- //
	// Shared between the reachability/verify loop (writer; owns the LAN trial
	// state machine) and the roster/punch handler closures (readers; use it to
	// skip raw-endpoint overwrites for any managed mode). When LAN direct is
	// disabled the loop only ever records modeDERP, so this degrades to exactly
	// the prior DERP-routed semantics.
	modes := newModeTracker()

	// ---- signaling client ---- //
	handler := newRosterHandler(tun, ps, bus, ipcState)
	handler.setModeTracker(modes)

	// ---- WAN punch-trial tracker (Phase 3) ---- //
	// Only meaningfully used when a DERP relay is configured (dBind != nil,
	// wired below) — a punch trial needs a DERP floor to fall back to on
	// timeout. Declared here (not inside the `if dBind != nil` block) so it's
	// in scope for the goroutine-start section further down.
	var punchTrials *punchTrialTracker

	// isSymmetricNAT reports whether this node's own NAT was classified as
	// symmetric by the one-shot dual-server STUN probe (see below). Read by
	// OnPunchCoord via handler.setIsSymmetricNATFn; atomic.Bool since it's
	// written once from the classification goroutine and read from the
	// signal-client goroutine.
	var isSymmetricNAT atomic.Bool

	// Wire managed-mode introspection so the roster + punch_coord handlers
	// know not to overwrite a managed endpoint (synthetic derp:<pubkey> for
	// modeDERP, or the direct ip:port for lan-trial/lan-direct/punch-trial/
	// direct) with a raw IP:port from a signal-driven update. nil-safe when
	// --derp-url isn't set: dBind stays nil and the gate returns false
	// (always allow direct updates).
	if dBind != nil {
		punchTrials = newPunchTrialTracker()
		handler.setDBind(dBind)
		handler.setPunchTrials(punchTrials)
		handler.setIsSymmetricNATFn(isSymmetricNAT.Load)

		handler.setIsDERPRoutedFn(func(pubkey string) bool {
			// "Managed" subsumes the old "DERP-routed" check: a peer in
			// lan-trial / lan-direct must ALSO be protected from raw-endpoint
			// overwrites, not just a DERP-routed one. modes is the source of
			// truth; dBind.IsPeerDERPRouted is folded in as a belt-and-braces
			// check for the steady-state DERP case.
			if modes.isManaged(pubkey) {
				return true
			}
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
		// inside flipToDERP, so this closure stays terse. Records modeDERP in
		// the tracker so the peer starts on the relay floor; the reachability
		// loop later promotes it to a LAN trial if a same-LAN candidate exists.
		handler.setFlipPeerToDERPFn(func(pubkey string) {
			kb, err := base64.StdEncoding.DecodeString(pubkey)
			if err != nil || len(kb) != 32 {
				slog.Warn("flip-on-roster: bad pubkey", "pubkey_prefix", util.TruncatePubkey(pubkey))
				return
			}
			var pub [32]byte
			copy(pub[:], kb)
			flipToDERP(tun, dBind, pubkey, pub)
			modes.set(pubkey, modeDERP)
		})
	}

	client := msignal.NewClient(cfg.SignalURL(), cfg.WGPublicKey, cfg.CoordinatorToken, coordClient.NodeToken, handler)

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
		// Propagate the (possibly freshly-enrolled) WG public key to the LIVE
		// signal client BEFORE prodding anything. client.wgPublicKey used to be
		// captured once at NewClient time with no update path — a /pair on an
		// already-running daemon (started unenrolled, so already looping on
		// backoff with hello.wg_public_key="") kept resending that stale
		// (usually empty) key forever, so the coordinator closed every
		// reconnect with node_not_enrolled until the whole process was
		// restarted. ipcState.WGPublicKey() was already updated by handlePair's
		// state.SetWelcome call before this closure runs (see localapi's
		// handlePair), so this always reads the current value.
		client.SetWGPublicKey(ipcState.WGPublicKey())
		// Send a no-op endpoint update to prod the existing connection. If the
		// new key differs, the coordinator will close with node_not_enrolled and
		// the client reconnects; on the next hello it now uses the key just set
		// above. No LAN candidates needed for this prod; the next STUN refresh
		// (or OnWelcome's own re-announce) carries them.
		client.SendEndpointUpdate(ipcState.MeshIP(), nil)
	}

	// (coordClient itself was already constructed earlier, alongside the
	// node-token load — see the "coordinator API client" section near the
	// top of run(), before the DERP bind that also needs it.)

	// applyCoordinatorURL installs newURL as the LIVE coordinator URL across
	// every coordinator-facing component, for a runtime override delivered
	// via PATCH /settings on the local IPC socket (internal/localapi's
	// handleSettingsPatch — the settings.json persistence + request
	// validation happen there; this closure only does the live-apply part):
	//
	//   - coordClient (internal/coordapi.Client): the single shared source
	//     of truth already used for the node-token propagation pattern —
	//     the DERP JWT fetcher reads the coordinator URL live from here too
	//     (see buildDERPBind/makeDERPJWTFetcher above), so updating it here
	//     is sufficient for both Enroll/ListPeers AND the next DERP token
	//     fetch.
	//   - the signal WS client's dial target (derived via
	//     signalURLFromCoordinatorURL).
	//   - ipcState, so GET /status reflects the new value immediately.
	//
	// Then prods a reconnect via the existing signalRestart mechanism
	// (best-effort — see its docstring above for exactly what that does and
	// does not guarantee).
	applyCoordinatorURL := func(newURL string) {
		coordClient.SetBaseURL(newURL)
		client.SetSignalURL(signalURLFromCoordinatorURL(newURL))
		ipcState.SetCoordinatorURL(newURL)
		signalRestart()
	}

	// ---- LAN pairing-token broadcast/listen ---- //
	lanSvc := lanpair.New()

	// ---- local IPC server ---- //
	ipcSrv := localapi.New(localapi.Config{
		Bus:         bus,
		PeerStore:   ps,
		State:       ipcState,
		PeerReachFn: tun.PeerReachability,
		IsDERPRoutedFn: func(pubkey string) bool {
			// UI semantics: "DERP-routed" means relayed, i.e. NOT direct. A
			// peer promoted to lan-direct is NOT DERP-routed even though the
			// dBind flag may still report stale state, so the tracker wins:
			// modeLANDirect / modeLANTrial → false. Unknown / modeDERP falls
			// through to the dBind flag (steady-state DERP).
			if modes.get(pubkey) != modeDERP {
				return false
			}
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
		CoordClient:           coordClient,
		SignalRestart:         signalRestart,
		WGPrivKeyPath:         cfg.WGPrivateKeyPath,
		SigningKeyPath:        cfg.SigningPrivateKeyPath,
		NodeTokenPath:         nodeTokenPath,
		SettingsPath:          settingsPath,
		ApplyCoordinatorURLFn: applyCoordinatorURL,
		SocketPath:            cfg.LocalAPISocketPath,
		Platform:              cfg.Platform,
		LanPair:               lanSvc,
	})

	// Discover this node's private-LAN endpoints once at startup so they show
	// in GET /status even before STUN resolves. Recomputed on every endpoint
	// change below (interfaces come + go). Observability only in this phase —
	// no routing decision uses these yet (Phase 2).
	ipcState.SetLocalEndpoints(localep.LocalCandidates(tun.WGPort()))

	// Publish endpoint changes to the bus from STUN.
	onEndpointChange := func(ep string) {
		ipcState.SetExternalEndpoint(ep)
		// Recompute LAN candidates: an interface may have come up/down since
		// the last refresh. RFC1918 only (never a tailnet address).
		local := localep.LocalCandidates(tun.WGPort())
		ipcState.SetLocalEndpoints(local)
		bus.Publish(events.NewEndpointChanged(ep))
		client.SendEndpointUpdate(ep, local)
	}

	// ---- STUN goroutine ---- //
	go runSTUNLoop(ctx, resolver, onEndpointChange, cfg.STUNRefreshInterval)

	// ---- NAT-type classification goroutine (Phase 3, recommended addition) ---- //
	// One-shot, best-effort: probes two DIFFERENT STUN servers from the SAME
	// local port and compares the external ports observed. A mismatch means
	// this node's own NAT allocates mappings per-destination (symmetric) —
	// client-side hole-punch cannot work from a symmetric NAT, so OnPunchCoord
	// (via isSymmetricNAT, wired above) skips starting new punch trials
	// entirely rather than burning punchTrialDuration (and a DERP-off window)
	// on every punch_coord in a doomed retry loop. NAT topology on a stable
	// network essentially never changes mid-session, so a single classification
	// at startup (bounded by classifyCtx's timeout below) is sufficient — no
	// periodic re-classification. Failure to classify (timeout, STUN servers
	// unreachable, etc.) just leaves isSymmetricNAT false: fail open to
	// attempting the trial, since the trial's own timeout+backoff already
	// bounds the cost of a failed attempt either way.
	if dBind != nil {
		go func() {
			classifyCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()
			natType, err := resolver.ClassifyNAT(classifyCtx)
			if err != nil {
				slog.Debug("stun: NAT classification failed, punch trials proceed unclassified",
					"err", err,
				)
				return
			}
			if natType == stun.NATSymmetric {
				isSymmetricNAT.Store(true)
				slog.Warn("stun: local NAT classified as SYMMETRIC — client-side hole-punch cannot work from this node; peers stay on the DERP relay floor")
			} else {
				slog.Info("stun: local NAT classified", "nat_type", natType.String())
			}
		}()
	}

	// ---- reachability + DERP-fallback + LAN-trial goroutine ---- //
	// myLocalIPNets is recomputed on each LAN trial decision (interfaces come +
	// go) via localep.LocalIPNets — passed as a provider so the loop always sees
	// the current interface set rather than a startup snapshot.
	go runReachabilityLoop(ctx, tun, ps, dBind, bus, modes, localep.LocalIPNets)

	// ---- WAN punch-trial verify/timeout/direct-health-check goroutine (Phase 3) ---- //
	// Runs unconditionally whenever a DERP relay is configured — NOT gated by
	// POTLUCK_LAN_DIRECT, since WAN direct is the general-purpose upgrade over
	// the always-on DERP floor. See landirect.go's Phase-3 doc comment.
	if dBind != nil {
		go runPunchTrialLoop(ctx, tun, ps, dBind, bus, modes, punchTrials)
	}

	// ---- roster reconcile goroutine ---- //
	// Safety net for roster_update broadcasts missed over the signal WS: the
	// coordinator only pushes roster deltas to currently-connected nodes, so a
	// node that was disconnected at the moment a peer enrolled stays stale until
	// a manual restart. This pulls the authoritative roster periodically and
	// reconciles against the local cache. See task 697458e9.
	go runRosterRefreshLoop(ctx, coordClient, ps, handler, ipcState)

	// ---- LAN pairing-token listener (receive-only) ---- //
	go lanSvc.RunListener(ctx)

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
	slog.Info("connecting to coordinator signal channel", "url", client.SignalURL())
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

// signalURLFromCoordinatorURL derives the /mesh/signal WebSocket URL from a
// coordinator base URL, upgrading http→ws and https→wss. Mirrors
// config.Config.SignalURL()'s transform exactly; kept as a free function
// here (rather than a *config.Config method call) so a runtime
// coordinator-URL override (PATCH /settings — see applyCoordinatorURL above)
// can compute the same value from an arbitrary string with no *config.Config
// in scope.
func signalURLFromCoordinatorURL(coordinatorURL string) string {
	u := strings.TrimRight(coordinatorURL, "/")
	u = strings.Replace(u, "http://", "ws://", 1)
	u = strings.Replace(u, "https://", "wss://", 1)
	return u + "/mesh/signal"
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

// derpFallbackThreshold is kept for reference in comments but is no longer
// used as the DERP-flip trigger. The flip now uses handshakeFreshWindow
// (!nowReachable, age >= 3min) so the DERP fallback condition is consistent
// with the reachability judgment used everywhere else. Previously this was
// 30s, which caused peers with handshake age 30s–3min to be incorrectly
// flipped to DERP even when the direct path was still live.
const derpFallbackThreshold = handshakeFreshWindow

// rosterRefreshInterval is the cadence of the roster reconciliation safety net.
// The signal WS is the fast path for real-time roster updates; this loop only
// catches deltas a node missed while briefly disconnected. The design caps this
// at 60s minimum to keep coordinator load low; 2 minutes balances freshness
// against traffic.
const rosterRefreshInterval = 2 * time.Minute

// runRosterRefreshLoop periodically pulls the authoritative household roster
// from the coordinator (GET /mesh/peers) and reconciles it against the local
// peer cache, synthesizing OnRosterUpdate calls for additions, endpoint changes,
// or removals that a WS broadcast missed. Diff-based: a clean cycle emits
// nothing (no event-bus flood). Durable fix for "a peer enrolled while I was
// disconnected and I never saw it without a restart" (task 697458e9).
func runRosterRefreshLoop(ctx context.Context, client *coordapi.Client, ps *peerstate.Store, handler *rosterHandler, ipcState *localapi.State) {
	ticker := time.NewTicker(rosterRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			reconcileRosterOnce(ctx, client, ps, handler, ipcState)
		}
	}
}

// reconcileRosterOnce pulls the coordinator roster once and applies any delta to
// the local cache + tunnel. Safe to run concurrently with the signal client's
// own OnRosterUpdate: the underlying cache/tunnel ops are mutex-guarded and
// idempotent, and the diff is recomputed from the live cache each cycle.
func reconcileRosterOnce(ctx context.Context, client *coordapi.Client, ps *peerstate.Store, handler *rosterHandler, ipcState *localapi.State) {
	myKey := ipcState.WGPublicKey()
	if myKey == "" {
		return // not enrolled yet — OnWelcome handles the initial roster
	}

	reqCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	remote, err := client.ListPeers(reqCtx, myKey)
	if err != nil {
		slog.Debug("roster refresh: list-peers failed (will retry next cycle)", "err", err)
		return
	}

	// Coordinator's view, keyed by node_id, excluding ourselves (the coordinator
	// roster includes the caller; the local cache holds only OTHER peers).
	remoteByID := make(map[string]msignal.RosterPeer, len(remote))
	for _, ep := range remote {
		if ep.WGPublicKey == myKey {
			continue
		}
		remoteByID[ep.NodeID] = enrolledToRosterPeer(ep)
	}

	localByID := make(map[string]msignal.RosterPeer)
	for _, p := range ps.All() {
		localByID[p.NodeID] = p
	}

	var u msignal.RosterUpdate
	for id, rp := range remoteByID {
		existing, ok := localByID[id]
		if !ok {
			u.Added = append(u.Added, rp)
		} else if rosterEndpointChanged(existing, rp) {
			u.Updated = append(u.Updated, rp)
		}
		// A changed wg_public_key (key rotation) is intentionally NOT reconciled
		// here: OnRosterUpdate's Updated path can't cleanly swap a peer's key,
		// and emitting add+remove for one node_id in a single update races on the
		// node_id-keyed cache. Key rotation still resolves on the next full
		// Welcome; this safety net targets add / remove / endpoint only.
	}
	for id := range localByID {
		if _, ok := remoteByID[id]; !ok {
			u.Removed = append(u.Removed, id)
		}
	}

	if len(u.Added) == 0 && len(u.Updated) == 0 && len(u.Removed) == 0 {
		return // in sync — emit nothing
	}

	slog.Info("roster refresh: reconciling updates missed over the signal WS",
		"added", len(u.Added), "updated", len(u.Updated), "removed", len(u.Removed))
	handler.OnRosterUpdate(u)
}

// enrolledToRosterPeer maps the coordinator's EnrolledPeer wire shape to the
// signal-layer RosterPeer used by the roster handler + peer cache.
func enrolledToRosterPeer(ep coordapi.EnrolledPeer) msignal.RosterPeer {
	return msignal.RosterPeer{
		NodeID:         ep.NodeID,
		NodeName:       ep.NodeName,
		MeshIP:         ep.MeshIP,
		WGPublicKey:    ep.WGPublicKey,
		LastEndpoint:   ep.LastEndpoint,
		LocalEndpoints: ep.LocalEndpoints,
		Platform:       ep.Platform,
	}
}

// rosterEndpointChanged reports whether a peer's tunnel-relevant endpoint moved.
// Treats both the external endpoint and the private-LAN candidate list as
// "update"-worthy so the safety-net reconcile keeps LAN candidates fresh, while
// a steady poll (nothing changed) still emits no events.
func rosterEndpointChanged(a, b msignal.RosterPeer) bool {
	return derefStr(a.LastEndpoint) != derefStr(b.LastEndpoint) ||
		!sameStrSlice(a.LocalEndpoints, b.LocalEndpoints)
}

// sameStrSlice reports element-wise equality of two string slices (order
// significant — LocalCandidates returns sorted output, so order is stable).
func sameStrSlice(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func derefStr(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// runReachabilityLoop polls the WG UAPI every 30s for each known peer's
// last-handshake time. It logs a message only on state transitions
// (unreachable→reachable or vice versa) and once per peer on startup.
//
// When dBind is non-nil (DERP relay is available), peers that are not reachable
// (no fresh handshake within handshakeFreshWindow) are flipped to DERP routing
// via dBind.SetPeerRouting. When a peer recovers (fresh handshake observed), it
// is flipped back to direct UDP.
//
// A peer is considered reachable when its last handshake is within
// handshakeFreshWindow (3 minutes). WireGuard renegotiates every ~3 minutes
// when traffic is flowing; a missed renegotiation means the peer is gone.
//
// Privacy: PeerReachability parses UAPI output internally and does not log
// raw IpcGet output (which would include the device's own private key).
func runReachabilityLoop(
	ctx context.Context,
	tun *tunnel.Tunnel,
	ps *peerstate.Store,
	dBind *derp.DerpBind,
	bus *events.Bus,
	modes *modeTracker,
	localIPNetsFn func() []net.IPNet,
) {
	// reachableState maps pubkey → last known reachable bool.
	// true = reachable, false = unreachable.
	// Peers not yet seen are treated as "unknown" (logged once as initial state).
	//
	// Phase 2 fields (only ever written when lanDirectEnabled()): mode tracks the
	// per-peer managed routing mode; the trial* fields drive the verified LAN
	// trial. When LAN direct is OFF, mode stays modeDERP and the trial code is
	// never reached, so behaviour is byte-for-byte the legacy DERP path.
	type peerReachState struct {
		reachable  bool
		seenOnce   bool
		derpRouted bool // true if we've flipped this peer to DERP routing

		// --- Phase 2 LAN-trial state ---
		mode              routingMode // derp (default) | lan-trial | lan-direct
		trialDeadline     time.Time   // when an in-progress trial gives up
		preTrialHandshake time.Time   // last-handshake snapshot at trial start
		backoffUntil      time.Time   // suppress re-trial until this time
		trialLanEp        string      // the LAN ip:port being trialled
	}
	states := make(map[string]*peerReachState)

	// maybeStartLANTrial promotes a modeDERP peer to a verified LAN trial when
	// (a) LAN direct is enabled, (b) the peer advertises a candidate co-subnet
	// with one of our interfaces, and (c) the peer is not in trial backoff.
	//
	// FORCE-REKEY (the crux of the fix): simply re-pointing the endpoint does NOT
	// make wireguard-go re-handshake — the DERP-established session stays valid
	// for ~2 min, so on an idle mesh no fresh handshake ever fires on the LAN
	// endpoint and the trial always times out. Instead we (1) turn DERP off for
	// this peer, (2) REMOVE the WG peer to clear the existing session, then (3)
	// re-ADD it pinned to the LAN endpoint with a SHORT persistent keepalive
	// (lanTrialKeepalive). The keepalive forces WG to bring up a session promptly
	// (a keepalive needs a live session → WG initiates a handshake to the LAN
	// endpoint right away), which the verify step can then observe as a handshake
	// NEWER than the pre-trial snapshot.
	//
	// Any failure rolls straight back to the DERP floor. No-op (and zero cost)
	// when LAN direct is disabled: shouldStartLANTrial short-circuits on
	// !enabled, so the legacy path is byte-for-byte unchanged.
	maybeStartLANTrial := func(st *peerReachState, p msignal.RosterPeer, pubkey string) {
		if !lanDirectEnabled() || dBind == nil {
			return
		}
		myNets := localIPNetsFn()
		lanEp, ok := sameLANEndpoint(myNets, p.LocalEndpoints)
		if !shouldStartLANTrial(true, st.mode, ok, st.backoffUntil, time.Now()) {
			return
		}

		kb, decErr := base64.StdEncoding.DecodeString(pubkey)
		if decErr != nil || len(kb) != 32 {
			return
		}
		var pub [32]byte
		copy(pub[:], kb)

		// Snapshot the pre-trial handshake from the live device BEFORE we clear
		// the session, so the verify step compares against the value as of trial
		// start (RemovePeer wipes the device's handshake; the freshness window in
		// evaluateLANTrial already guards the resulting zero-snapshot edge).
		preHS, _, _, _ := tun.PeerReachability(pubkey)

		// (1) DERP OFF for this peer so outbound packets to the LAN endpoint go
		// over the inner UDP bind, not the relay.
		dBind.SetPeerRouting(pub, false)
		// (2) Clear the existing (DERP-established) WG session.
		if err := tun.RemovePeer(pubkey); err != nil {
			slog.Warn("lan_trial: RemovePeer before re-pin failed, continuing",
				"node_id", p.NodeID,
				"pubkey_prefix", util.TruncatePubkey(pubkey),
				"err", err,
			)
		}
		// (3) Re-add pinned to the LAN endpoint with a short keepalive so WG
		// re-handshakes there PROMPTLY (even on an idle mesh). On a re-add failure,
		// restore the DERP floor (re-add for DERP + flip) rather than strand the
		// peer with no WG session at all.
		trialPeer := tunnel.Peer{
			PublicKey:           pubkey,
			AllowedIPs:          []string{p.MeshIP + "/32"},
			Endpoint:            lanEp,
			PersistentKeepalive: lanTrialKeepalive,
		}
		if err := tun.AddPeer(trialPeer); err != nil {
			slog.Error("lan_trial: AddPeer pinned to LAN failed, rolling back to DERP",
				"node_id", p.NodeID,
				"pubkey_prefix", util.TruncatePubkey(pubkey),
				"err", err,
			)
			restoreDERPFloor(tun, dBind, p, pubkey, pub)
			st.derpRouted = true
			st.mode = modeDERP
			modes.set(pubkey, modeDERP)
			return
		}

		st.mode = modeLANTrial
		st.preTrialHandshake = preHS
		st.trialDeadline = time.Now().Add(lanTrialDuration)
		st.trialLanEp = lanEp
		st.derpRouted = false
		modes.set(pubkey, modeLANTrial)

		slog.Info("lan_trial: started — DERP off, peer re-pinned to direct LAN endpoint (force-rekey)",
			"node_id", p.NodeID,
			"pubkey_prefix", util.TruncatePubkey(pubkey),
			"lan_endpoint", lanEp,
			"keepalive_s", lanTrialKeepalive,
			"deadline_ms", lanTrialDuration.Milliseconds(),
		)
	}

	// verifyTrials runs on the fast ticker. For each peer currently in a LAN
	// trial it checks whether a fresh handshake landed (→ promote to lan-direct)
	// or the deadline passed (→ roll back to the DERP floor + backoff). Cheap
	// when no peer is in trial: it scans the small states map and skips non-trial
	// entries. Never runs when LAN direct is disabled (no peer reaches modeLANTrial).
	verifyTrials := func() {
		now := time.Now()
		byKey := make(map[string]msignal.RosterPeer)
		for _, p := range ps.All() {
			byKey[p.WGPublicKey] = p
		}
		for pubkey, st := range states {
			if st.mode != modeLANTrial {
				continue
			}
			// If a rejoin re-sync reset the shared tracker to derp out from under
			// an in-progress trial, abandon the trial without acting on now-stale
			// device state — check() will reconcile the local st on its next tick.
			if modes != nil && modes.get(pubkey) == modeDERP {
				continue
			}
			p, inRoster := byKey[pubkey]
			if !inRoster {
				continue // peer left the roster; prune handles cleanup
			}
			lastHS, _, _, err := tun.PeerReachability(pubkey)
			if err != nil {
				continue
			}
			outcome := evaluateLANTrial(lastHS, st.preTrialHandshake, st.trialDeadline, now, handshakeFreshWindow)
			switch outcome {
			case trialPending:
				continue
			case trialVerified:
				// Stop keepalive-spamming a confirmed link: re-add the peer with
				// the standard mesh keepalive (25s), keeping it pinned to the
				// verified LAN endpoint. appendPeerConfig overwrites the existing
				// peer's keepalive (5s → 25s) without disturbing the live session,
				// so the direct path is not interrupted. Best-effort: a failure
				// here only means we keep the 5s trial keepalive, which is benign.
				directPeer := tunnel.Peer{
					PublicKey:           pubkey,
					AllowedIPs:          []string{p.MeshIP + "/32"},
					Endpoint:            st.trialLanEp,
					PersistentKeepalive: lanDirectKeepalive,
				}
				if err := tun.AddPeer(directPeer); err != nil {
					slog.Warn("lan_trial: restoring standard keepalive on verified peer failed (keeping trial keepalive)",
						"node_id", p.NodeID,
						"pubkey_prefix", util.TruncatePubkey(pubkey),
						"err", err,
					)
				}
				st.mode = modeLANDirect
				st.reachable = true
				st.derpRouted = false
				modes.set(pubkey, modeLANDirect)
				slog.Info("lan_trial: VERIFIED — peer reachable on direct LAN endpoint, staying direct",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"lan_endpoint", st.trialLanEp,
					"handshake_age", formatHandshakeAge(lastHS),
				)
				if bus != nil {
					bus.Publish(events.NewPeerUpdated(rosterPeerToSnapshot(p, true, false)))
				}
			case trialTimedOut:
				kb, decErr := base64.StdEncoding.DecodeString(pubkey)
				if decErr != nil || len(kb) != 32 {
					continue
				}
				var pub [32]byte
				copy(pub[:], kb)
				slog.Warn("lan_trial: TIMEOUT — no fresh handshake on LAN endpoint, restoring DERP floor + backoff",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"lan_endpoint", st.trialLanEp,
					"backoff_min", int(lanTrialBackoff.Minutes()),
				)
				if dBind != nil {
					// The trial cleared the session and re-added the peer pinned to
					// the (dead) LAN endpoint with a short keepalive. A bare
					// flipToDERP would only swap the endpoint/routing flag, leaving
					// the 5s trial keepalive in place; restoreDERPFloor re-adds the
					// peer with the standard config first, THEN flips, guaranteeing
					// a clean working relay session.
					restoreDERPFloor(tun, dBind, p, pubkey, pub)
				}
				st.mode = modeDERP
				st.derpRouted = true
				st.reachable = false
				st.trialLanEp = ""
				st.backoffUntil = now.Add(lanTrialBackoff)
				modes.set(pubkey, modeDERP)
			}
		}
	}

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

			st, known := states[pubkey]
			if !known {
				st = &peerReachState{}
				states[pubkey] = st
			}

			// Phase 3: peers owned by the WAN punch-trial mechanism
			// (runPunchTrialLoop, driven by OnPunchCoord — see landirect.go)
			// are skipped entirely here. This loop's own local st.mode never
			// reflects modePunchTrial/modeDirect (only the LAN trial code
			// below ever writes a non-derp value into it), so without this
			// guard the legacy DERP-fallback logic further down would see
			// "not reachable" — RemovePeer wipes the handshake the instant
			// OnPunchCoord force-rekeys the peer — and call flipToDERP,
			// silently undoing the punch trial's SetPeerRouting(false) out
			// from under it. Checked against the SHARED tracker (the source
			// of truth for WAN modes), not st.mode.
			if modes != nil {
				switch modes.get(pubkey) {
				case modePunchTrial, modeDirect:
					continue
				}
			}

			// Rejoin re-sync reconciliation (Phase 2, requirement #5): a same-key
			// rejoin in OnRosterUpdate tears down + re-adds the WG peer and
			// re-asserts DERP, resetting the shared tracker to modeDERP. If the
			// loop's local st still thinks the peer is lan-trial/lan-direct, the
			// device state has been wiped out from under it — adopt modeDERP and
			// clear ALL trial/backoff state so the trial re-runs cleanly against
			// the fresh WG peer. Only meaningful when LAN direct is enabled (the
			// only path that sets a non-derp local mode); a no-op otherwise.
			if modes != nil && st.mode != modeDERP && modes.get(pubkey) == modeDERP {
				slog.Info("lan_trial: rejoin re-sync detected, resetting peer to DERP + clearing trial state",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
				)
				st.mode = modeDERP
				st.trialDeadline = time.Time{}
				st.preTrialHandshake = time.Time{}
				st.backoffUntil = time.Time{}
				st.trialLanEp = ""
				st.derpRouted = true // OnRosterUpdate's flip re-asserted DERP
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

			// Phase 2: peers in a LAN-managed mode are handled separately from the
			// legacy DERP reachability/flip logic below.
			//
			//   - lan-trial: the fast verify ticker (verifyTrials) owns the
			//     verify/rollback decision with backoff. Skip the legacy logic
			//     entirely so a transiently-stale trial handshake doesn't get
			//     double-handled (the legacy DERP-flip branch would clobber the
			//     LAN endpoint mid-trial).
			//   - lan-direct: the peer is confirmed direct. If its handshake goes
			//     stale, the LAN path died — restore the DERP floor and apply
			//     backoff so we don't immediately re-trial a flapping path. While
			//     it stays fresh, leave it on the direct path (skip legacy flips).
			if st.mode == modeLANTrial {
				st.reachable = nowReachable
				continue
			}
			if st.mode == modeLANDirect {
				st.reachable = nowReachable
				// A live direct WG session only re-handshakes ~every 2min (REKEY_AFTER_TIME),
				// with keepalives in between, so judge a direct link dead by the 3min fresh
				// window (!nowReachable), NOT the 30s derpFallbackThreshold used for proactive
				// DERP fallback -- otherwise a perfectly healthy idle direct link false-positives
				// "stale" at 30s and bounces straight back to the relay.
				if !nowReachable {
					slog.Warn("lan_direct: direct LAN path went stale, restoring DERP floor",
						"node_id", p.NodeID,
						"pubkey_prefix", util.TruncatePubkey(pubkey),
						"last_handshake_ago", formatHandshakeAge(lastHS),
					)
					if peerDerpBind != nil {
						// The peer is still pinned to its (now-dead) LAN endpoint
						// from the verified trial. Re-add it with the standard DERP
						// config before flipping, so the relay session comes up
						// cleanly rather than inheriting the stale LAN pin.
						restoreDERPFloor(tun, peerDerpBind, p, pubkey, pubkeyBytes)
						st.derpRouted = true
					}
					st.mode = modeDERP
					st.trialLanEp = ""
					st.backoffUntil = time.Now().Add(lanTrialBackoff)
					modes.set(pubkey, modeDERP)
					if bus != nil {
						bus.Publish(events.NewPeerUpdated(rosterPeerToSnapshot(p, false, true)))
					}
				}
				continue
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
				// On first observation, if the peer is not reachable (handshake
				// age >= 3min or no handshake) and DERP is available, flip immediately.
				if peerDerpBind != nil && !nowReachable && !st.derpRouted {
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
					st.derpRouted = true
				}
				// Even on first observation, consider starting a LAN trial.
				maybeStartLANTrial(st, p, pubkey)
				continue
			}

			if nowReachable == st.reachable {
				// No reachability state transition — but still check DERP fallback
				// in case the peer is not reachable and needs DERP.
				if peerDerpBind != nil && !nowReachable && !st.derpRouted {
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
					st.derpRouted = true
				}
				// Steady-state DERP peer: this is the main entry point for
				// promoting a settled relay peer to a verified LAN trial.
				maybeStartLANTrial(st, p, pubkey)
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
				_ = peerDerpBind // intentionally not flipping back to direct

				// Same-key rejoin recovery: if this peer was already flipped to
				// DERP and just produced a FRESH handshake after a stale gap
				// (it went unreachable, then came back), the control plane
				// (handshakes/keepalives) is up again but the rejoin may have
				// disturbed the synthetic DERP endpoint + allowed-ip on the WG
				// device, so the DATA path stays broken. Re-assert the DERP
				// flip to re-install the synthetic endpoint and routing flag.
				// Idempotent for the steady-state case (peer was reachable all
				// along), so this only matters on the unreachable→reachable
				// transition handled here.
				if peerDerpBind != nil && st.derpRouted {
					slog.Info("derp_fallback: re-asserting DERP route after peer recovered",
						"node_id", p.NodeID,
						"pubkey_prefix", util.TruncatePubkey(pubkey),
					)
					flipToDERP(tun, peerDerpBind, pubkey, pubkeyBytes)
				}
				// Peer just recovered on DERP — eligible to attempt a LAN trial.
				maybeStartLANTrial(st, p, pubkey)
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
				// Peer lost: flip to DERP if available.
				if peerDerpBind != nil && !nowReachable && !st.derpRouted {
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
				modes.remove(k) // keep the shared tracker in sync with prune
			}
		}
	}

	// Run once immediately so startup logs show initial reachability.
	check()

	ticker := time.NewTicker(reachabilityCheckInterval)
	defer ticker.Stop()

	// Dedicated fast ticker for verifying in-progress LAN trials. Only started
	// when LAN direct is enabled; otherwise it never fires and verifyTrials is
	// unreachable, keeping the legacy path identical. The ticker channel is nil
	// when disabled, so its select case blocks forever (no spurious wakeups).
	var verifyC <-chan time.Time
	if lanDirectEnabled() {
		vt := time.NewTicker(lanVerifyInterval)
		defer vt.Stop()
		verifyC = vt.C
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			check()
		case <-verifyC:
			verifyTrials()
		}
	}
}

// restoreDERPFloor brings a peer that was clobbered by a LAN trial back to a
// clean, working DERP relay session — the SAFETY FLOOR after a failed (or
// stale) direct-LAN attempt.
//
// Why a plain flipToDERP is not enough here: the LAN trial REMOVED the WG
// session and re-ADDED the peer pinned to the LAN endpoint with a short (5s)
// trial keepalive. flipToDERP alone would only swap the endpoint to the
// synthetic address and set the routing flag, leaving that short keepalive (and
// any half-formed LAN session state) in place. Instead we re-add the peer from
// scratch with the standard mesh config (mesh-IP/32 allowed-ips, the standard
// 25s keepalive that DERP-routed peers require, and the last-known real endpoint
// as a hint), THEN flipToDERP to install the synthetic endpoint + routing flag.
// The result is byte-for-byte the same WG peer config a fresh roster-add would
// produce, so the relay path comes back exactly as it would on first contact.
//
// Kernel route invariant: maybeStartLANTrial calls tun.RemovePeer (which
// removes the WG peer from the device) but does NOT call tun.RemovePeerRoute
// (which would remove the kernel host route). The kernel route for this peer's
// mesh IP therefore remains installed pointing at the TUN interface throughout
// the trial and rollback. restoreDERPFloor does NOT need to call AddPeerRoute;
// the existing route is always present. This invariant must hold: if anything
// in the trial path ever starts calling RemovePeerRoute, a matching
// AddPeerRoute call must be added here.
//
// Correctness of the DERP floor: after this returns, (a) the peer exists on the
// WG device with allowed-ips = mesh-IP/32, (b) dBind has the peer marked
// DERP-routed, and (c) the WG endpoint is the synthetic DERP address so all
// outbound packets dispatch through DerpBind.Send to the relay. That is exactly
// the state a peer is in on normal DERP fallback, which is the proven-working
// relay path. A peer therefore always ends up reachable over DERP again after a
// failed trial.
func restoreDERPFloor(tun *tunnel.Tunnel, dBind *derp.DerpBind, p msignal.RosterPeer, pubkeyBase64 string, pubkeyBytes [32]byte) {
	// Re-add the peer with the standard mesh config (clears the trial's LAN pin
	// and short keepalive). rosterPeerToTunnelPeer supplies mesh-IP/32 +
	// 25s keepalive + the cached real endpoint; AddPeer's replace_allowed_ips
	// keeps the re-add idempotent. Best-effort: if this fails we still flip, so
	// the routing flag + synthetic endpoint are at least asserted.
	if err := tun.AddPeer(rosterPeerToTunnelPeer(p)); err != nil {
		slog.Warn("derp_fallback: re-add peer for DERP floor failed, flipping anyway",
			"node_id", p.NodeID,
			"pubkey_prefix", util.TruncatePubkey(pubkeyBase64),
			"err", err,
		)
	}
	flipToDERP(tun, dBind, pubkeyBase64, pubkeyBytes)
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

// promoteToDirect re-adds a peer pinned to a VERIFIED direct endpoint with the
// standard (non-trial) keepalive, superseding the previously dead-code
// flipToDirectUDP (which only cleared the DERP routing flag without pinning
// an endpoint — exactly the bare-endpoint-write bug #2 diagnoses, so wiring
// it up as-is would have reintroduced the same failure it was meant to fix).
// DERP routing for this peer must already be off by the time this is called
// (the trial that led here already turned it off via SetPeerRouting).
//
// Used by runPunchTrialLoop on a verified WAN punch trial. Best-effort: on
// AddPeer failure the peer is left on its trial (short) keepalive rather than
// stranded with no session — the caller logs and the next reachability check
// still sees a fresh handshake.
func promoteToDirect(tun *tunnel.Tunnel, pubkeyBase64, meshIP, endpoint string, keepalive int) error {
	return tun.AddPeer(tunnel.Peer{
		PublicKey:           pubkeyBase64,
		AllowedIPs:          []string{meshIP + "/32"},
		Endpoint:            endpoint,
		PersistentKeepalive: keepalive,
	})
}

// formatHandshakeAge returns a human-readable age string for a last-handshake
// time. Returns "<never>" for the zero value.
func formatHandshakeAge(t time.Time) string {
	if t.IsZero() {
		return "<never>"
	}
	return time.Since(t).Round(time.Second).String()
}

// runPunchTrialLoop is the Phase-3 WAN twin of runReachabilityLoop's LAN
// verify ticker. It owns two responsibilities, both scoped to peers OnPunchCoord
// put into modePunchTrial / promoted to modeDirect:
//
//  1. Verify in-progress punch trials: promote to modeDirect on a fresh
//     handshake newer than the pre-trial snapshot, or roll back to the DERP
//     floor + backoff on timeout. Mirrors runReachabilityLoop's verifyTrials
//     exactly (same evaluateLANTrial outcome type), just sourced from
//     punchTrials (cross-goroutine safe) instead of a loop-local map.
//  2. Health-check confirmed direct peers: a live direct WG session only
//     re-handshakes every ~2min (REKEY_AFTER_TIME) with keepalives in
//     between, so judge it dead by handshakeFreshWindow (3min) — NOT
//     punchVerifyInterval's fast 2s cadence — exactly the same reasoning
//     runReachabilityLoop's modeLANDirect branch documents.
//
// Runs unconditionally whenever dBind != nil — NOT gated by
// POTLUCK_LAN_DIRECT, since WAN punch is the general-purpose direct-path
// upgrade over the always-on DERP floor, independent of the opt-in same-LAN
// fast path.
//
// Peers currently owned by the LAN mechanism (modeLANTrial / modeLANDirect)
// are never touched here — modes.get returning anything other than
// modePunchTrial / modeDirect for a given peer means this loop has nothing to
// do for it this tick.
func runPunchTrialLoop(
	ctx context.Context,
	tun *tunnel.Tunnel,
	ps *peerstate.Store,
	dBind *derp.DerpBind,
	bus *events.Bus,
	modes *modeTracker,
	trials *punchTrialTracker,
) {
	tick := func() {
		now := time.Now()
		for _, p := range ps.All() {
			pubkey := p.WGPublicKey
			mode := modes.get(pubkey)
			if mode != modePunchTrial && mode != modeDirect {
				continue
			}

			kb, decErr := base64.StdEncoding.DecodeString(pubkey)
			if decErr != nil || len(kb) != 32 {
				continue
			}
			var pub [32]byte
			copy(pub[:], kb)

			lastHS, _, _, err := tun.PeerReachability(pubkey)
			if err != nil {
				continue
			}

			if mode == modeDirect {
				// Direct-health-check: revert to DERP on the SLOW (3min)
				// freshness window, not the fast trial-verify cadence — see
				// doc comment above.
				if !lastHS.IsZero() && time.Since(lastHS) < handshakeFreshWindow {
					continue
				}
				slog.Warn("punch_trial: confirmed direct WAN path went stale, restoring DERP floor",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"last_handshake_ago", formatHandshakeAge(lastHS),
				)
				restoreDERPFloor(tun, dBind, p, pubkey, pub)
				modes.set(pubkey, modeDERP)
				trials.setBackoff(pubkey, now.Add(punchTrialBackoff))
				if bus != nil {
					bus.Publish(events.NewPeerUpdated(rosterPeerToSnapshot(p, false, true)))
				}
				continue
			}

			// mode == modePunchTrial
			st, ok := trials.get(pubkey)
			if !ok {
				// Trial state missing (shouldn't happen — OnPunchCoord always
				// calls startTrial before setting modePunchTrial) — nothing to
				// evaluate against; wait for the next tick / a resync.
				continue
			}
			switch evaluateLANTrial(lastHS, st.preTrialHandshake, st.deadline, now, handshakeFreshWindow) {
			case trialPending:
				continue
			case trialVerified:
				if err := promoteToDirect(tun, pubkey, p.MeshIP, st.endpoint, punchDirectKeepalive); err != nil {
					slog.Warn("punch_trial: restoring standard keepalive on verified peer failed (keeping trial keepalive)",
						"node_id", p.NodeID,
						"pubkey_prefix", util.TruncatePubkey(pubkey),
						"err", err,
					)
				}
				modes.set(pubkey, modeDirect)
				slog.Info("punch_trial: VERIFIED — peer reachable direct cross-network, staying off DERP",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"endpoint", st.endpoint,
					"handshake_age", formatHandshakeAge(lastHS),
				)
				if bus != nil {
					bus.Publish(events.NewPeerUpdated(rosterPeerToSnapshot(p, true, false)))
				}
			case trialTimedOut:
				slog.Warn("punch_trial: TIMEOUT — no fresh handshake on punched endpoint, restoring DERP floor + backoff",
					"node_id", p.NodeID,
					"pubkey_prefix", util.TruncatePubkey(pubkey),
					"endpoint", st.endpoint,
					"backoff_min", int(punchTrialBackoff.Minutes()),
				)
				restoreDERPFloor(tun, dBind, p, pubkey, pub)
				modes.set(pubkey, modeDERP)
				trials.setBackoff(pubkey, now.Add(punchTrialBackoff))
			}
		}

		// Prune trial bookkeeping for peers no longer in the roster (mirrors
		// runReachabilityLoop's states prune).
		live := make(map[string]bool, len(ps.All()))
		for _, p := range ps.All() {
			live[p.WGPublicKey] = true
		}
		trials.prune(live)
	}

	ticker := time.NewTicker(punchVerifyInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			tick()
		}
	}
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

	// modes is the shared per-peer routing-mode tracker (Phase 2/3). Used here
	// both to RESET a peer's mode to derp on a same-key rejoin re-sync (so the
	// LAN trial re-runs cleanly against the freshly re-added WG peer) and, as
	// of Phase 3, to gate/drive the WAN punch trial in OnPunchCoord. nil when
	// the tracker isn't wired (it always is in main; nil-safe regardless).
	modes *modeTracker

	// dBind is the DERP bind, needed directly (not just via the isDERPRoutedFn/
	// flipPeerToDERPFn closures) so OnPunchCoord can turn DERP OFF for a peer
	// before force-rekeying it to the punched endpoint (SetPeerRouting) and
	// restoreDERPFloor can turn it back on on trial timeout. nil when
	// --derp-url isn't set — the punch-trial mechanism requires a DERP floor
	// to fall back to, so it is a no-op (bare UpdatePeerEndpoint, the
	// pre-Wave-1 behaviour) in that configuration; see OnPunchCoord.
	dBind *derp.DerpBind

	// punchTrials is the WAN punch-trial bookkeeping shared with
	// runPunchTrialLoop (see landirect.go's punchTrialTracker doc). nil when
	// dBind is nil (same gating as dBind above).
	punchTrials *punchTrialTracker

	// isSymmetricNATFn reports whether THIS node's own NAT has been classified
	// as symmetric (external port varies per destination) — see
	// internal/stun's dual-server NAT classifier. When true, OnPunchCoord
	// skips starting new punch trials entirely: a symmetric NAT on our own
	// side means our external mapping for a handshake-init to peer A differs
	// from the mapping the coordinator/STUN observed, so any trial is a
	// guaranteed failure. nil (never classified yet, or classification
	// disabled) is treated as "not symmetric" — fail open to attempting the
	// trial, since an unclassified NAT is the common case at startup before
	// the one-shot classifier completes.
	isSymmetricNATFn func() bool

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

// setModeTracker wires the shared Phase-2 routing-mode tracker. Called in main
// before client.Run (no data race). nil-safe everywhere it's read.
func (h *rosterHandler) setModeTracker(m *modeTracker) {
	h.modes = m
}

// setDBind wires the DERP bind directly (Phase 3 — WAN punch trial). Called
// in main only when --derp-url is set (dBind != nil); left nil otherwise, in
// which case OnPunchCoord degrades to a bare UpdatePeerEndpoint (no DERP
// floor to protect / fall back to).
func (h *rosterHandler) setDBind(b *derp.DerpBind) {
	h.dBind = b
}

// setPunchTrials wires the shared WAN punch-trial tracker (Phase 3). Called
// in main alongside setDBind, using the same nil-safety story.
func (h *rosterHandler) setPunchTrials(pt *punchTrialTracker) {
	h.punchTrials = pt
}

// setIsSymmetricNATFn wires the NAT-classification query (Phase 3). nil-safe:
// when never set (e.g. classification hasn't completed, or is unavailable),
// OnPunchCoord treats every peer as not-symmetric — see the field doc above.
func (h *rosterHandler) setIsSymmetricNATFn(fn func() bool) {
	h.isSymmetricNATFn = fn
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

	// 1b. Re-announce OUR OWN current endpoint on every fresh connection
	// (first connect AND every reconnect). Without this, a node whose STUN
	// resolution completes before the signal WS finishes its hello/welcome
	// handshake has its very first SendEndpointUpdate silently dropped
	// ("signal: no active connection, dropping endpoint_update" — sendCh is
	// nil until OnWelcome fires) and then NEVER retries, because the STUN
	// loop only calls SendEndpointUpdate again when the resolved endpoint
	// CHANGES (see runSTUNLoop) — on a stable network that may be hours or
	// days. The same gap re-opens on every signal reconnect (network blip,
	// coordinator restart): the external IP hasn't changed, so STUN stays
	// silent, and the peer we're reconnecting to never learns we're back.
	// ipcState already holds the last-known values regardless of whether the
	// original send actually reached the coordinator, so replaying them here
	// is always safe and idempotent (coordinator just re-stores + re-
	// broadcasts the same value). Skipped when we have no endpoint yet
	// (fresh process, STUN hasn't resolved) — the STUN loop's own first-
	// resolve call handles that case.
	if h.ipcState != nil && h.client != nil {
		if ep := h.ipcState.ExternalEndpoint(); ep != "" {
			h.client.SendEndpointUpdate(ep, h.ipcState.LocalEndpoints())
		}
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
		// A coordinator "added" event is NOT always a brand-new peer: when a
		// peer reboots and rejoins with its SAME persisted WG key, the
		// coordinator re-broadcasts it as `added`. If we already know this
		// node locally, treat the event as a clean RE-SYNC rather than a
		// fresh add: tear the WG peer down and re-add it (now safe — fix in
		// appendPeerConfig emits replace_allowed_ips, so the re-add cleanly
		// replaces the stale allowed-ips instead of merging) so the data path
		// re-establishes. A plain re-add over stale state black-holes traffic.
		_, alreadyKnown := h.ps.Get(p.NodeID)
		h.ps.Upsert(p)
		peer := rosterPeerToTunnelPeer(p)

		if alreadyKnown {
			slog.Info("roster update: same-key rejoin detected, re-syncing peer",
				"node_id", p.NodeID,
				"mesh_ip", p.MeshIP,
			)
			// Drop the stale WG peer first so the re-add starts from a clean
			// slate (clears any stale endpoint/allowed-ip state on the device).
			if err := h.tun.RemovePeer(p.WGPublicKey); err != nil {
				slog.Warn("tunnel: RemovePeer during rejoin re-sync failed",
					"node_id", p.NodeID,
					"err", err,
				)
			}
		}

		if err := h.tun.AddPeer(peer); err != nil {
			slog.Error("tunnel: AddPeer failed",
				"node_id", p.NodeID,
				"mesh_ip", p.MeshIP,
				"err", err,
			)
		}
		// Install the kernel route for the new peer's mesh IP. Re-asserting an
		// existing route on a rejoin is a no-op/idempotent at the OS layer.
		if p.MeshIP != "" {
			if err := h.tun.AddPeerRoute(p.MeshIP); err != nil {
				slog.Warn("tunnel: AddPeerRoute failed for new peer",
					"node_id", p.NodeID,
					"mesh_ip", p.MeshIP,
					"err", err,
				)
			}
		}
		// Proactively flip to DERP for the peer (same rationale as
		// OnWelcome — don't waste 30s on direct-UDP handshakes that
		// can't succeed under hairpin/symmetric NAT). On a rejoin re-sync
		// this re-asserts the synthetic DERP endpoint + routing flag that the
		// fresh AddPeer just cleared, so the relay data path comes back.
		if h.flipPeerToDERPFn != nil {
			h.flipPeerToDERPFn(p.WGPublicKey)
		}
		if h.bus != nil {
			h.bus.Publish(events.NewPeerAdded(rosterPeerToSnapshot(p, false, false)))
		}
		// Newly added (or rejoined) peer: punch if endpoint known.
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
		// Clean up WAN punch-trial bookkeeping immediately (runPunchTrialLoop's
		// periodic prune is a safety net, not the primary cleanup path).
		if h.punchTrials != nil {
			h.punchTrials.remove(cached.WGPublicKey)
		}
		if h.modes != nil {
			h.modes.remove(cached.WGPublicKey)
		}
	}
}

// OnPunchCoord is called when the coordinator delivers a punch_coord message
// to this node. Both the initiator and the responder start a force-rekey
// punch TRIAL pinned to the peer's endpoint, so WireGuard's handshake-init
// fires from both sides concurrently, punching through both NAT mappings
// simultaneously. A verified fresh handshake promotes the peer off DERP to
// direct routing (modeDirect); a trial timeout rolls it straight back to the
// DERP floor with a backoff before the next attempt.
//
// If peer_endpoint is nil, the peer has not STUN'd yet — log a warning and
// skip; we'll get another punch_coord when the peer's endpoint is known.
//
// This fixes the bug where the peer was proactively (and permanently) flipped
// to DERP on welcome/roster-add BEFORE this fires, and the old guard skipped
// the endpoint write whenever the peer was DERP-routed — which, given the
// proactive flip, was always. See landirect.go's Phase-3 doc comment for the
// full before/after.
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

	// No DERP bind configured at all (--derp-url unset): there is no floor to
	// protect and no trial/verify machinery wired up (dBind/punchTrials/modes
	// are only set alongside it in main). Degrade to the pre-Wave-1 behaviour:
	// a bare endpoint write. This is the only case where we still call
	// UpdatePeerEndpoint directly from here.
	if h.dBind == nil || h.punchTrials == nil || h.modes == nil {
		if err := h.tun.UpdatePeerEndpoint(coord.PeerWGPublicKey, *coord.PeerEndpoint); err != nil {
			slog.Error("tunnel: UpdatePeerEndpoint failed in punch_coord handler (no DERP configured)",
				"peer_node_id", coord.PeerNodeID,
				"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
				"err", err,
			)
		}
		return
	}

	// A peer already in a managed direct-path mode (LAN-owned OR already
	// trialling/confirmed-direct over WAN) is never re-trialled from here —
	// let whichever mechanism owns it finish. This replaces the old permanent
	// "DERP-routed → skip" veto with a narrow one that only skips genuinely
	// in-flight or already-successful work.
	switch h.modes.get(coord.PeerWGPublicKey) {
	case modeLANTrial, modeLANDirect:
		slog.Debug("punch_coord: peer is LAN-managed, deferring to the LAN-direct trial",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
		)
		return
	case modePunchTrial, modeDirect:
		slog.Debug("punch_coord: peer already has an active/confirmed WAN punch trial, skipping duplicate start",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
		)
		return
	}

	kb, decErr := base64.StdEncoding.DecodeString(coord.PeerWGPublicKey)
	if decErr != nil || len(kb) != 32 {
		slog.Warn("punch_coord: could not decode peer pubkey, skipping",
			"peer_node_id", coord.PeerNodeID,
			"err", decErr,
		)
		return
	}
	var pub [32]byte
	copy(pub[:], kb)

	symmetric := h.isSymmetricNATFn != nil && h.isSymmetricNATFn()
	st, _ := h.punchTrials.get(coord.PeerWGPublicKey)
	now := time.Now()
	if !shouldStartPunchTrial(h.modes.get(coord.PeerWGPublicKey), st.backoffUntil, symmetric, now) {
		slog.Debug("punch_coord: trial suppressed (backoff or local NAT classified symmetric)",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
			"symmetric_nat", symmetric,
			"backoff_remaining", time.Until(st.backoffUntil).Round(time.Second),
		)
		return
	}

	p, ok := h.ps.Get(coord.PeerNodeID)
	if !ok {
		slog.Warn("punch_coord: peer not in local cache, cannot start trial",
			"peer_node_id", coord.PeerNodeID,
		)
		return
	}

	// Snapshot the pre-trial handshake BEFORE the RemovePeer below wipes it,
	// exactly like maybeStartLANTrial — evaluateLANTrial's freshness guard
	// handles the resulting zero-snapshot edge case.
	preHS, _, _, _ := h.tun.PeerReachability(coord.PeerWGPublicKey)

	// (1) DERP OFF for this peer so outbound packets to the punched endpoint
	// go over the inner UDP bind, not the relay.
	h.dBind.SetPeerRouting(pub, false)
	// (2) Clear the existing (DERP-established) WG session — this is the
	// force-rekey fix: a bare UpdatePeerEndpoint does NOT make wireguard-go
	// re-handshake on a live session (see tunnel.go's UpdatePeerEndpoint doc
	// and the same-key-rejoin history in commit 45b6390).
	if err := h.tun.RemovePeer(coord.PeerWGPublicKey); err != nil {
		slog.Warn("punch_coord: RemovePeer before re-pin failed, continuing",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
			"err", err,
		)
	}
	// (3) Re-add pinned to the punched endpoint with a short keepalive so WG
	// initiates a handshake-init toward it PROMPTLY, even on an idle mesh.
	// The peer does the same simultaneously (it also received a punch_coord),
	// so when both handshake-inits cross in flight, the NATs on both sides
	// record the mapping and the next inbound packet traverses cleanly.
	trialPeer := tunnel.Peer{
		PublicKey:           coord.PeerWGPublicKey,
		AllowedIPs:          []string{p.MeshIP + "/32"},
		Endpoint:            *coord.PeerEndpoint,
		PersistentKeepalive: punchTrialKeepalive,
	}
	if err := h.tun.AddPeer(trialPeer); err != nil {
		slog.Error("punch_coord: AddPeer pinned to punched endpoint failed, rolling back to DERP",
			"peer_node_id", coord.PeerNodeID,
			"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
			"err", err,
		)
		restoreDERPFloor(h.tun, h.dBind, p, coord.PeerWGPublicKey, pub)
		h.modes.set(coord.PeerWGPublicKey, modeDERP)
		return
	}

	h.modes.set(coord.PeerWGPublicKey, modePunchTrial)
	h.punchTrials.startTrial(coord.PeerWGPublicKey, *coord.PeerEndpoint, preHS, now.Add(punchTrialDuration))

	slog.Info("punch_coord: WAN direct trial started — DERP off, peer re-pinned to punched endpoint (force-rekey)",
		"peer_node_id", coord.PeerNodeID,
		"pubkey_prefix", util.TruncatePubkey(coord.PeerWGPublicKey),
		"endpoint", *coord.PeerEndpoint,
		"role", coord.Role,
		"keepalive_s", punchTrialKeepalive,
		"deadline_ms", punchTrialDuration.Milliseconds(),
	)
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
//
// coordURLFn returns the CURRENT coordinator base URL — called on every
// token fetch (not just once here at construction) so a runtime
// coordinator-URL override (PATCH /settings; see internal/localapi's
// handleSettingsPatch and this file's applyCoordinatorURL) reaches the DERP
// JWT fetcher the same way a freshly-minted node_token reaches it: read
// live from coordapi.Client, never captured as a snapshot string. Pass
// coordClient.BaseURL.
//
// nodeTokenFn returns the current per-node bearer credential (may be "" pre-
// enroll), or nil if the caller has none to offer. See coordapi.Client.NodeToken.
func buildDERPBind(cfg *config.Config, coordURLFn func() string, nodeTokenFn func() string) (*derp.DerpBind, *derp.Client) {
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
		makeDERPJWTFetcher(coordURLFn, cfg.WGPublicKey, cfg.CoordinatorToken, cfg.SigningPrivateKeyPath, nodeTokenFn),
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

// derpTokenRegion is the single v0 DERP region. Shared between the request
// body ("region") and the PoP-signed message (which must carry the SAME
// region string — see signingkey.DERPTokenMessage / routers/mesh.py's
// _verify_derp_pop).
const derpTokenRegion = "ord"

// buildDERPTokenRequestBody constructs the POST /mesh/derp-token JSON body.
// It ALWAYS carries wg_public_key + region. It additionally carries the
// OPTIONAL proof-of-possession nonce+signature, but ONLY when this node's
// current signing key is confirmed registered with the coordinator (see
// internal/signingkey's package doc: the coordinator 401s the ENTIRE request
// if a signature is present but no signing_public_key is registered for this
// node, so an upgraded-but-not-yet-(re)paired node must omit both fields and
// fall back to the legacy wg_public_key-possession-only shape it already
// used before this change — that fallback is what keeps DERP working with
// POTLUCK_DERP_POP_REQUIRED off, which is the current and only-safe-so-far
// prod state).
func buildDERPTokenRequestBody(wgPublicKey, region, signingKeyPath string) map[string]string {
	body := map[string]string{
		"wg_public_key": wgPublicKey,
		"region":        region,
	}

	kp, err := signingkey.LoadOrGenerate(signingKeyPath)
	if err != nil {
		slog.Warn("derp: signing keypair unavailable, requesting token without PoP", "err", err)
		return body
	}
	if !signingkey.IsRegistered(signingKeyPath, kp.PublicKeyB64) {
		// Not (yet) registered with the coordinator — sign nothing. This is
		// the normal, safe steady state for any node that hasn't (re)enrolled
		// since this feature shipped.
		return body
	}

	nonce := strconv.FormatInt(time.Now().Unix(), 10)
	body["nonce"] = nonce
	body["signature"] = kp.Sign(signingkey.DERPTokenMessage(wgPublicKey, region, nonce))
	return body
}

// makeDERPJWTFetcher returns a JWTFetcher that calls the coordinator's
// POST /mesh/derp-token endpoint to get a fresh auth token.
//
// coordURLFn is called on every fetch (not just once at construction) so a
// runtime coordinator-URL override (PATCH /settings) is picked up on the
// very next token refresh — see buildDERPBind's doc for why this must be a
// live getter rather than a captured string.
//
// nodeTokenFn is called on every fetch (not just once at construction) so a
// token minted by a /pair that happens after this fetcher is built — DERP is
// wired up before enrollment, at process start — is picked up on the very
// next refresh with no separate propagation call needed.
func makeDERPJWTFetcher(coordURLFn func() string, wgPublicKey, coordinatorToken, signingKeyPath string, nodeTokenFn func() string) derp.JWTFetcher {
	return func(ctx context.Context) (string, error) {
		coordinatorURL := coordURLFn()
		body := buildDERPTokenRequestBody(wgPublicKey, derpTokenRegion, signingKeyPath)
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
		// Primary credential: the per-node bearer, fetched live. Legacy
		// coordinator token stays attached whenever configured, unconditionally
		// alongside it (belt-and-suspenders / dev-fleet back-compat).
		if nodeTokenFn != nil {
			if nt := nodeTokenFn(); nt != "" {
				req.Header.Set("Authorization", "Bearer "+nt)
			}
		}
		if coordinatorToken != "" {
			req.Header.Set("X-Potluck-Coordinator-Token", coordinatorToken)
		}

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
