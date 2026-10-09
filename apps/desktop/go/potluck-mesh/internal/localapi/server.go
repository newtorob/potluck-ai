// Package localapi implements the local IPC HTTP+WS server that Electron uses
// to query mesh state and drive the pairing flow.
//
// Transport: Unix domain socket on macOS/Linux, named pipe on Windows.
// Protocol:  HTTP/1.1 over the socket. JSON request/response bodies.
//
//	WebSocket upgrade for WS /events.
//
// Endpoints (all return JSON):
//
//	GET   /status    — current sidecar state
//	GET   /peers     — household roster with reachability
//	POST  /pair      — enroll this node using a pairing token
//	POST  /unpair    — wipe local keys + stop tunnel
//	PATCH /settings  — runtime settings override (currently: coordinator_url)
//	WS    /events    — subscribe to the event bus
//
// Every response includes X-Potluck-Protocol-Version: 1 per the spec.
//
// Auth model: filesystem ACLs only — the socket is 0600 (Unix) or user-DACL
// (Windows). No bearer tokens or per-request signatures.
//
// Privacy: never logs WG private keys, pairing tokens, or inference content.
// Peer metadata (mesh IPs, pubkey prefixes, hostnames) is fine — it is
// mesh-layer information.
package localapi

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/lanpair"
	"github.com/potluck-ai/potluck-mesh/internal/nodetoken"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/runtimesettings"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/signingkey"
)

// protocolVersion is the value of the X-Potluck-Protocol-Version response
// header sent on every response, per docs/phase-g/local-ipc.md.
const protocolVersion = "1"

// MeshStatus describes the current mesh state.
type MeshStatus string

const (
	StatusStarting MeshStatus = "starting"
	StatusRunning  MeshStatus = "running"
	StatusStopped  MeshStatus = "stopped"
	StatusError    MeshStatus = "error"
)

// ReachabilityMode is the current path used to reach a peer.
type ReachabilityMode string

const (
	ReachDirect ReachabilityMode = "direct"
	ReachDERP   ReachabilityMode = "derp"
	// ReachConnecting reports a peer that this node has flipped to DERP
	// routing (SetPeerRouting(pubkey, true) on the local DerpBind) but for
	// which no WireGuard handshake has completed yet — either the handshake
	// is still in flight over the relay, or the relay itself is unreachable.
	// Without this value, that state was indistinguishable from "never tried
	// direct yet": both read as reachability_mode: null. New enum value —
	// additive, existing "direct"/"derp"/null consumers are unaffected.
	ReachConnecting ReachabilityMode = "connecting"
)

// PeerReachabilityFunc is the tunnel's reachability query function. Matches
// the signature of tunnel.Tunnel.PeerReachability so main.go can pass it
// directly without an interface.
type PeerReachabilityFunc func(pubkeyBase64 string) (lastHandshake time.Time, rxBytes, txBytes uint64, err error)

// IsDERPRoutedFunc reports whether a peer is currently routed via the DERP relay.
type IsDERPRoutedFunc func(pubkeyBase64 string) bool

// IsPeerConfiguredFunc reports whether the WG device has the given peer's key
// configured — used to tell a drifted/unprovisioned key from a firewall block.
type IsPeerConfiguredFunc func(pubkeyBase64 string) bool

// SignalRestartFunc is called after /pair to re-connect the signal client with
// the newly enrolled WG key.
type SignalRestartFunc func()

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

// State holds live sidecar state that the server reads to answer /status.
// All fields are protected by mu; use the provided setters from main.go.
type State struct {
	mu sync.RWMutex

	meshStatus           MeshStatus
	nodeID               string
	householdID          string
	meshIP               string
	wgPublicKey          string
	externalEndpoint     string
	localEndpoints       []string // private-LAN "ip:port" candidates (observability)
	coordinatorConnState string   // "connected"|"connecting"|"disconnected"
	lastError            string
	coordinatorURL       string // static launch-flag value; never empty
	wgPort               int    // UDP port the WireGuard device listens on (default 51820)
	features             []string
}

// NewState returns an idle State in the "starting" phase.
func NewState() *State {
	return &State{
		meshStatus:           StatusStarting,
		coordinatorConnState: "connecting",
	}
}

func (s *State) SetMeshStatus(st MeshStatus) {
	s.mu.Lock()
	s.meshStatus = st
	s.mu.Unlock()
}

// SetWelcome is called when the signal client receives a welcome from the
// coordinator. It marks the mesh as running.
func (s *State) SetWelcome(nodeID, householdID, meshIP, wgPublicKey string) {
	s.mu.Lock()
	s.nodeID = nodeID
	s.householdID = householdID
	s.meshIP = meshIP
	s.wgPublicKey = wgPublicKey
	s.meshStatus = StatusRunning
	s.coordinatorConnState = "connected"
	s.mu.Unlock()
}

func (s *State) SetExternalEndpoint(ep string) {
	s.mu.Lock()
	s.externalEndpoint = ep
	s.mu.Unlock()
}

// SetLocalEndpoints records this node's discovered private-LAN candidates
// ("ip:port") for observability via GET /status. Observability only — no
// routing effect in this phase. The slice is copied so the caller may reuse it.
func (s *State) SetLocalEndpoints(eps []string) {
	cp := append([]string(nil), eps...)
	s.mu.Lock()
	s.localEndpoints = cp
	s.mu.Unlock()
}

// SetFeatures records the optional capabilities GET /status reports (see
// signal.FeatureCircleLinkV1). Set it once at startup, before serving.
func (s *State) SetFeatures(features []string) {
	cp := append([]string(nil), features...)
	s.mu.Lock()
	s.features = cp
	s.mu.Unlock()
}

func (s *State) SetCoordinatorConnState(st string) {
	s.mu.Lock()
	s.coordinatorConnState = st
	s.mu.Unlock()
}

// SetCoordinatorURL records the coordinator URL the sidecar is dialing.
// Originally a static value (the --coordinator-url launch flag / a
// persisted runtimesettings override) set once at startup before the server
// begins serving; also called live by handleSettingsPatch after a
// successful PATCH /settings so GET /status reflects a runtime override
// immediately.
func (s *State) SetCoordinatorURL(u string) {
	s.mu.Lock()
	s.coordinatorURL = u
	s.mu.Unlock()
}

// CoordinatorURL returns the coordinator URL the sidecar is currently
// dialing (or about to dial, if not yet enrolled). Used by
// handleSettingsPatch to echo the current value when a PATCH /settings body
// doesn't touch coordinator_url.
func (s *State) CoordinatorURL() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.coordinatorURL
}

// SetWGPort records the UDP port the WireGuard device is listening on. Call
// once at startup after the tunnel is created. Used by the /status readiness
// probe to check whether the WG transport socket is actually bound.
func (s *State) SetWGPort(port int) {
	s.mu.Lock()
	s.wgPort = port
	s.mu.Unlock()
}

func (s *State) SetLastError(msg string) {
	s.mu.Lock()
	s.lastError = msg
	s.meshStatus = StatusError
	s.mu.Unlock()
}

func (s *State) SetWGPublicKey(pubkey string) {
	s.mu.Lock()
	s.wgPublicKey = pubkey
	s.mu.Unlock()
}

// ClearEnrollment wipes the in-memory enrollment identity (node_id,
// household_id, mesh_ip, wg_public_key, external_endpoint) and moves
// the status back to Stopped. Called from handleUnpair so the next
// GET /status reflects an actually-unpaired node — otherwise the
// frontend sees stale node_id + mesh_ip and concludes the node is
// still paired.
func (s *State) ClearEnrollment() {
	s.mu.Lock()
	s.nodeID = ""
	s.householdID = ""
	s.meshIP = ""
	s.wgPublicKey = ""
	s.externalEndpoint = ""
	s.localEndpoints = nil
	s.lastError = ""
	s.meshStatus = StatusStopped
	s.mu.Unlock()
}

// WGPublicKey returns the current WG public key (base64).
func (s *State) WGPublicKey() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.wgPublicKey
}

// MeshIP returns the current mesh IP (empty if not enrolled).
func (s *State) MeshIP() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.meshIP
}

// ExternalEndpoint returns this node's last STUN-discovered public endpoint
// ("ip:port"), or "" if none has resolved yet. Used to re-announce our own
// current endpoint on every fresh signal connection (see
// cmd/potluck-mesh/main.go's rosterHandler.OnWelcome) — closing the race
// where the initial SendEndpointUpdate fires before the signal WS has
// finished connecting and is silently dropped ("no active connection").
func (s *State) ExternalEndpoint() string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.externalEndpoint
}

// LocalEndpoints returns a copy of this node's last-discovered private-LAN
// candidates. See ExternalEndpoint's doc for why a live getter (rather than
// only the STUN-loop's own change-triggered send) is needed.
func (s *State) LocalEndpoints() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return append([]string(nil), s.localEndpoints...)
}

// snapshot returns a JSON-serialisable view. peerCount is supplied by the
// caller (from peerstate.Store.All()) to avoid a nested lock.
//
// The readiness probes (vpn_conflict, wg_port_listening) are computed here
// outside the read-lock because they call net.Interfaces / net.ListenPacket,
// which may themselves acquire internal locks. Capturing the relevant state
// fields under the lock and computing the probes after releasing it avoids
// any risk of lock ordering issues.
func (s *State) snapshot(peerCount int) statusResponse {
	// Capture mutable fields under the read-lock.
	s.mu.RLock()
	coordConnState := s.coordinatorConnState
	wgPort := s.wgPort
	resp := statusResponse{
		Version:               "0.1.0",
		MeshStatus:            string(s.meshStatus),
		CoordinatorConnection: coordConnState,
		PeersOnlineCount:      peerCount,
		CoordinatorURL:        s.coordinatorURL,
		Features:              append([]string{}, s.features...),
	}
	if s.nodeID != "" {
		v := s.nodeID
		resp.NodeID = &v
	}
	if s.householdID != "" {
		v := s.householdID
		resp.HouseholdID = &v
	}
	if s.meshIP != "" {
		v := s.meshIP
		resp.MeshIP = &v
	}
	if s.wgPublicKey != "" {
		v := s.wgPublicKey
		resp.WGPublicKey = &v
	}
	if s.externalEndpoint != "" {
		v := s.externalEndpoint
		resp.ExternalEndpoint = &v
	}
	if len(s.localEndpoints) > 0 {
		resp.LocalEndpoints = append([]string(nil), s.localEndpoints...)
	}
	if s.lastError != "" {
		v := s.lastError
		resp.LastError = &v
	}
	s.mu.RUnlock()

	// Readiness probes — computed after releasing the lock (no network calls
	// under a lock; these are cheap stdlib operations only).

	// VPN conflict probe: enumerate interfaces for known third-party VPN
	// names. Conflict is only flagged when the VPN is up AND the coordinator
	// is not connected (the Mullvad kill-switch failure pattern).
	vpn := checkVPNConflict(coordConnState)
	resp.VPNConflict = vpn.Conflict
	resp.VPNDetail = vpn.Detail

	// WireGuard port probe: is our UDP transport socket actually bound?
	// When wgPort is 0 (SetWGPort not yet called — tunnel not yet up),
	// report false rather than probing port 0.
	if wgPort > 0 {
		resp.WGPortListening = checkWGPortListening(wgPort)
	}

	return resp
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

// Config carries all dependencies wired in from main.go.
type Config struct {
	Bus                *events.Bus
	PeerStore          *peerstate.Store
	State              *State
	PeerReachFn        PeerReachabilityFunc // may be nil if tunnel isn't up yet
	IsDERPRoutedFn     IsDERPRoutedFunc     // may be nil
	IsPeerConfiguredFn IsPeerConfiguredFunc // may be nil
	CoordClient        *coordapi.Client
	SignalRestart      SignalRestartFunc
	WGPrivKeyPath      string           // path to detect / generate the WG private key
	SigningKeyPath     string           // path to the Ed25519 DERP-token PoP signing key (internal/signingkey)
	NodeTokenPath      string           // path to the persisted per-node bearer credential (internal/nodetoken); "" disables persistence (tests)
	SocketPath         string           // "" → use platform default
	Platform           string           // runtime.GOOS
	LanPair            *lanpair.Service // LAN pairing-token broadcast/listen; may be nil

	// Circle is the holder of direct circle links (GET /circle-peers, and what
	// POST /unpair clears). nil when this build holds none.
	Circle CircleSource

	// SettingsPath is where PATCH /settings persists a runtime coordinator-URL
	// override (internal/runtimesettings). "" disables persistence (tests) —
	// the live update via ApplyCoordinatorURLFn still happens, it just won't
	// survive a restart.
	SettingsPath string
	// ApplyCoordinatorURLFn is called after a validated, persisted PATCH
	// /settings coordinator_url update to install the new value as the LIVE
	// coordinator URL across coordClient, the signal client, and ipcState
	// (see cmd/potluck-mesh/main.go's applyCoordinatorURL). nil is
	// tolerated (tests, or a caller that only wants the value reflected in
	// State without any live side effects) — handleSettingsPatch falls back
	// to State.SetCoordinatorURL alone in that case.
	ApplyCoordinatorURLFn func(url string)
}

// Server is the local IPC HTTP+WS server.
type Server struct {
	bus                   *events.Bus
	ps                    *peerstate.Store
	state                 *State
	peerReachFn           PeerReachabilityFunc
	isDERPRoutedFn        IsDERPRoutedFunc
	isPeerConfiguredFn    IsPeerConfiguredFunc
	coordClient           *coordapi.Client
	signalRestart         SignalRestartFunc
	wgPrivKeyPath         string
	signingKeyPath        string
	nodeTokenPath         string
	platform              string
	socketPath            string
	lanpair               *lanpair.Service
	circle                CircleSource
	settingsPath          string
	applyCoordinatorURLFn func(url string)

	// pairingMu prevents concurrent /pair attempts.
	pairingMu sync.Mutex

	// enrolled is set atomically to 1 after a successful /pair.
	enrolled atomic.Int32

	httpSrv *http.Server
}

// New constructs a Server. Call Serve(ctx) to start accepting connections.
func New(cfg Config) *Server {
	sp := cfg.SocketPath
	if sp == "" {
		sp = defaultSocketPath()
	}

	s := &Server{
		bus:                   cfg.Bus,
		ps:                    cfg.PeerStore,
		state:                 cfg.State,
		peerReachFn:           cfg.PeerReachFn,
		isDERPRoutedFn:        cfg.IsDERPRoutedFn,
		isPeerConfiguredFn:    cfg.IsPeerConfiguredFn,
		coordClient:           cfg.CoordClient,
		signalRestart:         cfg.SignalRestart,
		wgPrivKeyPath:         cfg.WGPrivKeyPath,
		signingKeyPath:        cfg.SigningKeyPath,
		nodeTokenPath:         cfg.NodeTokenPath,
		platform:              cfg.Platform,
		socketPath:            sp,
		lanpair:               cfg.LanPair,
		circle:                cfg.Circle,
		settingsPath:          cfg.SettingsPath,
		applyCoordinatorURLFn: cfg.ApplyCoordinatorURLFn,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("GET /peers", s.handlePeers)
	mux.HandleFunc("GET /circle-peers", s.handleCirclePeers)
	mux.HandleFunc("POST /pair", s.handlePair)
	mux.HandleFunc("POST /unpair", s.handleUnpair)
	mux.HandleFunc("GET /events", s.handleEvents)
	mux.HandleFunc("POST /pairing/broadcast", s.handlePairingBroadcast)
	mux.HandleFunc("GET /pairing/discovered", s.handlePairingDiscovered)
	mux.HandleFunc("POST /pairing/reveal", s.handlePairingReveal)
	mux.HandleFunc("PATCH /settings", s.handleSettingsPatch)

	// Wrap every response with the protocol-version header.
	versionMW := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("X-Potluck-Protocol-Version", protocolVersion)
			next.ServeHTTP(w, r)
		})
	}

	s.httpSrv = &http.Server{
		Handler:           versionMW(mux),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	return s
}

// SocketPath returns the path the server will listen on.
func (s *Server) SocketPath() string { return s.socketPath }

// Serve creates the platform-appropriate listener and starts the HTTP server.
// Blocks until ctx is cancelled, then performs a graceful shutdown (sends a
// terminal "status_changed: stopped" event, drains connections, removes socket).
func (s *Server) Serve(ctx context.Context) error {
	ln, err := platformListen(s.socketPath)
	if err != nil {
		return err
	}

	slog.Info("localapi: IPC server listening",
		"path", s.socketPath,
		"platform", runtime.GOOS,
	)

	// Determine "enrolled" at startup. A WG keypair existing on disk is NOT
	// proof of enrollment: ensureKeypair generates one unconditionally the
	// first time the tunnel needs it (bring-up happens before /pair is ever
	// called), so a freshly-installed, never-paired machine ALREADY has a
	// wg.key file by the time this runs — the old `os.Stat(wgPrivKeyPath)`
	// check alone treated that as "enrolled" and made POST /pair 409 with
	// already_enrolled before ever contacting the coordinator (in-app "Add a
	// machine" would 409 on a fresh machine). The persisted node_token
	// (internal/nodetoken) is the authoritative signal instead: it is minted
	// ONLY by a successful POST /mesh/enroll (see handlePair below) and
	// cleared on /unpair, so its presence is a true enrollment record rather
	// than a side effect of key generation.
	//
	// When node_token persistence isn't configured (nodeTokenPath == "" —
	// tests, or a build predating node-token support), fall back to the
	// legacy wg-key-exists heuristic so behaviour is unchanged there.
	if s.nodeTokenPath != "" {
		if tok, err := nodetoken.Load(s.nodeTokenPath); err == nil && tok != "" {
			s.enrolled.Store(1)
		}
	} else if _, statErr := os.Stat(s.wgPrivKeyPath); statErr == nil {
		s.enrolled.Store(1)
	}

	srvErr := make(chan error, 1)
	go func() {
		if err := s.httpSrv.Serve(ln); err != nil && err != http.ErrServerClosed {
			srvErr <- err
		} else {
			srvErr <- nil
		}
	}()

	select {
	case <-ctx.Done():
		// Notify WS subscribers about the impending shutdown before we
		// close the server (so they get the event, not a broken pipe).
		s.bus.PublishWithTimeout(events.NewStatusChanged(string(StatusStopped)), 500*time.Millisecond)

		shutCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = s.httpSrv.Shutdown(shutCtx) // graceful drain; we're shutting down regardless

		// Clean up Unix socket file.
		if runtime.GOOS != "windows" {
			_ = os.Remove(s.socketPath) // best-effort; stale socket will be removed on next start
		}
		return ctx.Err()

	case err := <-srvErr:
		return err
	}
}

// ---------------------------------------------------------------------------
// JSON wire types
// ---------------------------------------------------------------------------

type statusResponse struct {
	Version               string   `json:"version"`
	MeshStatus            string   `json:"mesh_status"`
	NodeID                *string  `json:"node_id"`
	HouseholdID           *string  `json:"household_id"`
	MeshIP                *string  `json:"mesh_ip"`
	WGPublicKey           *string  `json:"wg_public_key"`
	ExternalEndpoint      *string  `json:"external_endpoint"`
	LocalEndpoints        []string `json:"local_endpoints,omitempty"`
	CoordinatorConnection string   `json:"coordinator_connection"`
	CoordinatorURL        string   `json:"coordinator_url"`
	PeersOnlineCount      int      `json:"peers_online_count"`
	LastError             *string  `json:"last_error"`

	// Readiness probes (added for netcheck view).

	// VPNConflict is true when a known third-party VPN interface (e.g. Mullvad)
	// is detected AND the coordinator connection is not "connected". This
	// combination is the Mullvad kill-switch failure pattern that took down the
	// mesh in a real incident. False when the VPN is up but the mesh is still
	// connected (low-severity; surfaced in VPNDetail only).
	VPNConflict bool `json:"vpn_conflict"`
	// VPNDetail is a human-readable sentence describing what was found.
	// Empty when no third-party VPN is detected.
	VPNDetail string `json:"vpn_detail"`

	// WGPortListening is true when the WireGuard UDP transport port (51820 by
	// default) is observed as bound on the local machine. A false value means
	// the tunnel socket is not up — either the WireGuard device failed to start
	// or the port was stolen by another process.
	WGPortListening bool `json:"wg_port_listening"`

	// PeersConnectingCount is the number of roster peers this node has
	// flipped to DERP routing (see peerResponse.DERPFlipped) that have not
	// yet completed a WireGuard handshake — i.e. reachability_mode
	// "connecting" in GET /peers. A whole-node summary so a systray/global
	// indicator doesn't need to walk the peer list itself. Additive field.
	PeersConnectingCount int `json:"peers_connecting_count"`

	// Features lists the optional capabilities of this sidecar, for example
	// "circle_link_v1" (the circle_peer_* messages and GET /circle-peers). A
	// consumer that needs one checks for its name; an older sidecar has no such
	// key at all. Always a list, empty when there are none.
	Features []string `json:"features"`
}

// scopeHousehold marks a peer that belongs to this machine's owner. Every peer
// the sidecar can hold today is one. A consumer that treats a listed peer as
// one of the owner's own machines (the engine's /peer/* gate, prompt routing)
// must count only the household scope, so a peer of another kind can be added
// later without being mistaken for one.
const scopeHousehold = "household"

type peerResponse struct {
	NodeID           string   `json:"node_id"`
	NodeName         string   `json:"node_name"`
	MeshIP           string   `json:"mesh_ip"`
	WGPublicKey      string   `json:"wg_public_key"`
	Scope            string   `json:"scope"`
	LastEndpoint     *string  `json:"last_endpoint,omitempty"`
	LocalEndpoints   []string `json:"local_endpoints,omitempty"`
	LastSeenAt       *string  `json:"last_seen_at,omitempty"`
	Platform         *string  `json:"platform,omitempty"`
	Reachable        bool     `json:"reachable"`
	ReachabilityMode *string  `json:"reachability_mode"`
	// DERPFlipped reports whether this node currently has this peer marked as
	// DERP-routed (DerpBind.SetPeerRouting flip via IsPeerDERPRouted),
	// independent of whether a WireGuard handshake has completed over that
	// route yet. Additive field — safe for older frontends to ignore. Lets
	// the UI (and debugging) distinguish "flipped to DERP, handshake pending
	// or relay unreachable" (reachability_mode: "connecting") from "never
	// flipped, still trying direct" (reachability_mode: null, derp_flipped:
	// false) — both previously reported as an indistinguishable null.
	DERPFlipped bool `json:"derp_flipped"`
	// HandshakeAgeSeconds is the age of the last WireGuard handshake. nil when
	// no handshake has ever completed (lets the UI tell "stale" from "never").
	HandshakeAgeSeconds *float64 `json:"handshake_age_seconds,omitempty"`
	// KeyConfigured reports whether this peer's key is provisioned on the WG
	// device. nil when unknown (tunnel down). false + unreachable = key drift.
	KeyConfigured *bool `json:"key_configured,omitempty"`
}

type peersResponse struct {
	Peers []peerResponse `json:"peers"`
}

type pairRequest struct {
	PairingToken string `json:"pairing_token"`
	NodeName     string `json:"node_name,omitempty"`
}

type errorBody struct {
	Error errorDetail `json:"error"`
}

type errorDetail struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// ---------------------------------------------------------------------------
// Request handlers
// ---------------------------------------------------------------------------

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	roster := s.ps.All()
	snap := s.state.snapshot(len(roster))
	snap.PeersConnectingCount = s.countPeersConnecting(roster)
	writeJSON(w, http.StatusOK, snap)
}

// countPeersConnecting mirrors the reachability_mode computation in
// handlePeers, but only tallies peers in the "connecting" state (DERP-flipped
// on this node, handshake not yet complete) — see peerResponse.DERPFlipped
// and ReachConnecting for why that state needs to be observable at all.
func (s *Server) countPeersConnecting(roster []signal.RosterPeer) int {
	if s.isDERPRoutedFn == nil {
		return 0
	}
	n := 0
	for _, p := range roster {
		if !s.isDERPRoutedFn(p.WGPublicKey) {
			continue
		}
		reachable := false
		if s.peerReachFn != nil {
			lastHS, _, _, err := s.peerReachFn(p.WGPublicKey)
			if err == nil {
				reachable = !lastHS.IsZero() && time.Since(lastHS) < 3*time.Minute
			}
		}
		if !reachable {
			n++
		}
	}
	return n
}

func (s *Server) handlePeers(w http.ResponseWriter, r *http.Request) {
	roster := s.ps.All()
	out := make([]peerResponse, 0, len(roster))

	for _, p := range roster {
		pr := peerResponse{
			NodeID:      p.NodeID,
			NodeName:    p.NodeName,
			MeshIP:      p.MeshIP,
			WGPublicKey: p.WGPublicKey,
			Scope:       scopeHousehold,
		}
		if p.LastEndpoint != nil {
			pr.LastEndpoint = p.LastEndpoint
		}
		if len(p.LocalEndpoints) > 0 {
			pr.LocalEndpoints = append([]string(nil), p.LocalEndpoints...)
		}
		if p.Platform != nil {
			pr.Platform = p.Platform
		}

		// Reachability: query the tunnel's last-handshake age. We expose the
		// age explicitly (not just the reachable bool) so the UI can tell a
		// connection that dropped recently (stale) from one that never formed.
		if s.peerReachFn != nil {
			lastHS, _, _, err := s.peerReachFn(p.WGPublicKey)
			if err == nil {
				if !lastHS.IsZero() {
					age := time.Since(lastHS).Seconds()
					pr.HandshakeAgeSeconds = &age
				}
				pr.Reachable = !lastHS.IsZero() && time.Since(lastHS) < 3*time.Minute
			}
		}

		// Key provisioned on the device? Distinguishes a drifted/unprovisioned
		// key from a firewall/NAT block when the peer is unreachable.
		if s.isPeerConfiguredFn != nil {
			kc := s.isPeerConfiguredFn(p.WGPublicKey)
			pr.KeyConfigured = &kc
		}

		// Reachability mode: DERP vs direct vs connecting.
		//
		// derp_flipped is independent of the handshake state — it reflects
		// this node's local DerpBind routing decision, which can be true well
		// before (or even without) a completed handshake: the relay may still
		// be unreachable, or the responder's side of the handshake may not
		// have converged yet (see internal/derp.DerpBind.Send's doc comment
		// for the wg-go endpoint-roaming race this exposes). Surfacing it
		// separately from reachability_mode turns that previously-invisible
		// window into an observable "connecting" state instead of a null
		// that's indistinguishable from "hasn't tried DERP at all".
		derpFlipped := s.isDERPRoutedFn != nil && s.isDERPRoutedFn(p.WGPublicKey)
		pr.DERPFlipped = derpFlipped

		switch {
		case pr.Reachable && derpFlipped:
			mode := string(ReachDERP)
			pr.ReachabilityMode = &mode
		case pr.Reachable:
			mode := string(ReachDirect)
			pr.ReachabilityMode = &mode
		case derpFlipped:
			mode := string(ReachConnecting)
			pr.ReachabilityMode = &mode
		}
		// Neither reachable nor flipped: ReachabilityMode stays nil (JSON
		// null) — still trying direct, unchanged from prior behavior.

		out = append(out, pr)
	}

	writeJSON(w, http.StatusOK, peersResponse{Peers: out})
}

func (s *Server) handlePair(w http.ResponseWriter, r *http.Request) {
	// Fast path: already enrolled.
	if s.enrolled.Load() == 1 {
		writeError(w, http.StatusConflict, "already_enrolled",
			"this node is already enrolled; call POST /unpair first to re-pair")
		return
	}

	// Serialise concurrent attempts (a pairing wizard double-submit, etc.).
	if !s.pairingMu.TryLock() {
		writeError(w, http.StatusConflict, "pair_in_progress",
			"a pairing attempt is already in progress")
		return
	}
	defer s.pairingMu.Unlock()

	// Re-check under the lock.
	if s.enrolled.Load() == 1 {
		writeError(w, http.StatusConflict, "already_enrolled",
			"this node is already enrolled; call POST /unpair first to re-pair")
		return
	}

	var req pairRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return
	}
	if req.PairingToken == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "pairing_token is required")
		return
	}

	// Determine node name: supplied or hostname + 3-hex-char suffix.
	nodeName := req.NodeName
	if nodeName == "" {
		hn, _ := os.Hostname()
		if hn == "" {
			hn = "potluck-node"
		}
		nodeName = hn + "-" + shortID()
	}

	// Generate (or load) WG keypair.
	wgPublicKey, err := ensureKeypair(s.wgPrivKeyPath)
	if err != nil {
		slog.Error("localapi: pair: keypair error", "err", err)
		writeError(w, http.StatusInternalServerError, "keypair_failed",
			"could not generate WG keypair: "+err.Error())
		return
	}

	// Generate (or load) this node's Ed25519 signing keypair — DISTINCT from
	// the WG keypair above, used only for the DERP-token PoP challenge. A
	// failure here is logged but does NOT fail pairing: signing_public_key is
	// optional on the wire (coordinator/schemas/mesh.py), so a node that
	// can't produce one simply enrolls without it and falls back to the
	// legacy DERP-token auth path forever (safe, just unhardened for that
	// one node).
	var signingPubKey *string
	signingKP, skErr := signingkey.LoadOrGenerate(s.signingKeyPath)
	if skErr != nil {
		slog.Warn("localapi: pair: signing keypair unavailable, enrolling without PoP support",
			"err", skErr)
	} else {
		signingPubKey = &signingKP.PublicKeyB64
	}

	// Call the coordinator's enroll endpoint.
	platform := s.platform
	enrollReq := coordapi.EnrollRequest{
		PairingToken:     req.PairingToken,
		WGPublicKey:      wgPublicKey,
		NodeName:         nodeName,
		Platform:         &platform,
		SigningPublicKey: signingPubKey,
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	enrollResp, err := s.coordClient.Enroll(ctx, enrollReq)
	if err != nil {
		if ce, ok := err.(*coordapi.CoordinatorError); ok {
			slog.Warn("localapi: pair: coordinator error",
				"http_status", ce.StatusCode,
				"body_prefix", truncate(ce.Body, 128),
			)
			httpStatus := ce.StatusCode
			if httpStatus >= 500 {
				httpStatus = http.StatusBadGateway
			}
			writeError(w, httpStatus, "coordinator_error", ce.Body)
			return
		}
		slog.Error("localapi: pair: coordinator request failed", "err", err)
		writeError(w, http.StatusBadGateway, "coordinator_unreachable", err.Error())
		return
	}

	// The coordinator accepted this enrollment — if we sent a
	// signing_public_key, it is now registered there. This is the ONLY point
	// in the node's lifecycle where that becomes true; record it locally so
	// the DERP-token fetcher knows it's safe to start signing (see
	// internal/signingkey's package doc for why this ordering matters).
	if signingPubKey != nil {
		if mErr := signingkey.MarkRegistered(s.signingKeyPath, *signingPubKey); mErr != nil {
			// Non-fatal: the coordinator DOES have the key registered now, we
			// just failed to record that fact locally. Worst case this node
			// stays on the legacy (unsigned) DERP-token path until the marker
			// write can succeed on a future pairing — never a false-positive
			// "registered" that could trigger an unregistered-signature 401.
			slog.Warn("localapi: pair: failed to persist signing-key registration marker",
				"err", mErr)
		}
	}

	// Persist the freshly-minted per-node bearer credential (shown exactly
	// ONCE in this response — see coordapi.EnrollResponse.NodeToken / the
	// coordinator's EnrollResponse docstring) to disk, mode 0600, the same
	// directory as the WG key. Then make it the LIVE credential on the
	// shared coordapi.Client: every subsequent heartbeat/peers/derp-token
	// call and the signal WS's next reconnect all read it from there (see
	// cmd/potluck-mesh/main.go's wiring — coordClient.NodeToken is passed as
	// a live getter to both), so this one call is the only propagation this
	// handler needs to do.
	if s.nodeTokenPath != "" {
		if err := nodetoken.Save(s.nodeTokenPath, enrollResp.NodeToken); err != nil {
			// Non-fatal: the coordinator DID mint + hash-store this token —
			// we only failed to persist our own copy. Worst case this node
			// keeps using the legacy coordinator-token fallback (if
			// configured) until a future re-pair succeeds in writing the
			// file; never block enrollment on a local disk-write failure the
			// user can't act on mid-flow.
			slog.Warn("localapi: pair: failed to persist node_token to disk", "err", err)
		}
	}
	s.coordClient.SetNodeToken(enrollResp.NodeToken)

	// Persist state and mark enrolled.
	s.state.SetWelcome(
		enrollResp.NodeID,
		enrollResp.HouseholdID,
		enrollResp.MeshIP,
		wgPublicKey,
	)
	s.enrolled.Store(1)

	slog.Info("localapi: node enrolled successfully",
		"node_id", enrollResp.NodeID,
		"household_id", enrollResp.HouseholdID,
		"mesh_ip", enrollResp.MeshIP,
		"peer_count", len(enrollResp.Peers),
	)

	// Notify WS subscribers.
	s.bus.Publish(events.NewStatusChanged(string(StatusRunning)))

	// Reconnect the signal client so it hellos with the new WG key.
	if s.signalRestart != nil {
		go s.signalRestart()
	}

	writeJSON(w, http.StatusCreated, enrollResp)
}

func (s *Server) handleUnpair(w http.ResponseWriter, r *http.Request) {
	if err := os.Remove(s.wgPrivKeyPath); err != nil && !os.IsNotExist(err) {
		slog.Error("localapi: unpair: remove private key failed", "err", err)
		writeError(w, http.StatusInternalServerError, "unpair_failed",
			"could not remove WG private key: "+err.Error())
		return
	}

	// Wipe the signing keypair + its registration marker too, so a
	// subsequent /pair generates a fresh Ed25519 identity and starts from a
	// clean (unregistered) state — it only starts signing DERP-token
	// requests again once that fresh /pair actually confirms registration.
	// Best-effort: a failure here doesn't block unpair (the WG key removal
	// above is what actually severs the node from the household), but it
	// does mean a stale signing key could linger — log so it's visible.
	if s.signingKeyPath != "" {
		if err := os.Remove(s.signingKeyPath); err != nil && !os.IsNotExist(err) {
			slog.Warn("localapi: unpair: remove signing key failed", "err", err)
		}
		if err := signingkey.ClearRegistered(s.signingKeyPath); err != nil {
			slog.Warn("localapi: unpair: clear signing-key registration marker failed", "err", err)
		}
	}

	// Wipe the persisted node_token too, so a subsequent /pair mints (and
	// this sidecar persists) a completely fresh one rather than a stale
	// credential left over from this enrollment. Also clear the LIVE value
	// on the shared coordapi.Client so no in-flight or newly-dialed request
	// keeps presenting a token for a node the coordinator no longer
	// considers ours — the legacy coordinator token (if configured) is the
	// only credential in play again until the next successful /pair.
	if s.nodeTokenPath != "" {
		if err := nodetoken.Clear(s.nodeTokenPath); err != nil {
			slog.Warn("localapi: unpair: remove node token failed", "err", err)
		}
	}
	if s.coordClient != nil {
		s.coordClient.SetNodeToken("")
	}

	s.enrolled.Store(0)
	s.state.ClearEnrollment()
	// A node that leaves its household must not keep a stranger's peer.
	if s.circle != nil {
		s.circle.Clear()
	}
	s.bus.Publish(events.NewStatusChanged(string(StatusStopped)))

	// TODO(week7): call coordinator DELETE /mesh/nodes/{id} to deregister.
	// Until that endpoint exists, the coordinator GC's stale nodes automatically.
	// Signal client teardown is handled by the signalRestart mechanism in main.go.
	slog.Info("localapi: node unpaired, WG private key wiped")

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusNoContent)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		// Electron renders from file:// or localhost:5173 — skip origin check.
		InsecureSkipVerify: true,
	})
	if err != nil {
		slog.Warn("localapi: WS /events upgrade failed", "err", err)
		return
	}
	defer conn.CloseNow() // best-effort cleanup; error intentionally ignored

	ch, unsub := s.bus.Subscribe()
	defer unsub()

	ctx := r.Context()
	slog.Debug("localapi: WS /events client connected", "remote", r.RemoteAddr)

	for {
		select {
		case <-ctx.Done():
			conn.Close(websocket.StatusNormalClosure, "server shutdown")
			return

		case evt, ok := <-ch:
			if !ok {
				// Bus closed — the sidecar is shutting down.
				// Best-effort: send a final status event before closing. Error ignored
				// because we're closing immediately after regardless.
				_ = wsjson.Write(ctx, conn, events.NewStatusChanged(string(StatusStopped)))
				conn.Close(websocket.StatusNormalClosure, "sidecar shutdown")
				return
			}
			if err := wsjson.Write(ctx, conn, evt); err != nil {
				slog.Debug("localapi: WS write error (client disconnected)", "err", err)
				return
			}
		}
	}
}

// ---------------------------------------------------------------------------
// LAN pairing-token broadcast / discovery
// ---------------------------------------------------------------------------

const (
	// How long an inviter broadcasts after the wizard opens. Short on purpose:
	// the coordinator token may live an hour, but it only needs to be on the
	// LAN for the moment the user is actively adding a machine.
	pairingBroadcastWindow = 120 * time.Second
	// A discovered invite is only offered if heard within this window.
	pairingDiscoveryMaxAge = 150 * time.Second
	// How long POST /pairing/reveal waits for the inviter to answer a
	// PIN-gated reveal request before giving up.
	pairingRevealTimeout = 5 * time.Second
)

// handlePairingBroadcast starts broadcasting a pairing invite on the LAN. Only
// an enrolled machine can invite (it is the one that issued the token); an
// unenrolled machine has no token to share.
//
// The LAN announce itself never carries the raw, redeemable token (see
// internal/lanpair's package doc) — only a one-way fingerprint of it. The
// response here additively includes a freshly generated confirmation_pin:
// the caller is expected to display it locally (e.g. on the inviter's
// screen) so a human can read it aloud/over-the-shoulder to whoever is
// joining. Without that PIN, a machine that only overheard the LAN broadcast
// cannot complete POST /pairing/reveal and so cannot obtain a redeemable
// token from a passive capture alone.
func (s *Server) handlePairingBroadcast(w http.ResponseWriter, r *http.Request) {
	if s.lanpair == nil {
		writeError(w, http.StatusServiceUnavailable, "lan_unavailable", "LAN pairing is not available")
		return
	}
	if s.enrolled.Load() != 1 {
		writeError(w, http.StatusConflict, "not_enrolled", "only an enrolled machine can broadcast a pairing token")
		return
	}
	var body struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Token == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "token is required")
		return
	}
	hostname, _ := os.Hostname()
	pin := s.lanpair.StartBroadcast(body.Token, hostname, pairingBroadcastWindow)
	// Never log the PIN or the token: the PIN is the out-of-band secret this
	// whole mechanism depends on, same rule as the WG keys / pairing tokens
	// noted at the top of this file.
	slog.Info("localapi: broadcasting pairing invite on LAN", "window_s", int(pairingBroadcastWindow.Seconds()))
	writeJSON(w, http.StatusOK, map[string]any{
		"broadcasting":     true,
		"window_seconds":   int(pairingBroadcastWindow.Seconds()),
		"confirmation_pin": pin,
	})
}

// handlePairingDiscovered returns an invite heard on the LAN, if any. Only
// meaningful on an unenrolled machine that is looking to join; an enrolled
// machine never acts on a discovered invite.
//
// This intentionally no longer returns a redeemable token (it never learned
// one — the LAN announce doesn't carry it). It returns a fingerprint the
// caller can use with POST /pairing/reveal once a human has supplied the
// inviter's confirmation PIN.
func (s *Server) handlePairingDiscovered(w http.ResponseWriter, r *http.Request) {
	if s.lanpair == nil || s.enrolled.Load() == 1 {
		writeJSON(w, http.StatusOK, map[string]any{"found": false})
		return
	}
	fingerprint, hostname, ok := s.lanpair.Discovered(pairingDiscoveryMaxAge)
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{"found": false})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"found":        true,
		"hostname":     hostname,
		"fingerprint":  fingerprint,
		"pin_required": true,
	})
}

type pairingRevealRequest struct {
	Fingerprint string `json:"fingerprint"`
	PIN         string `json:"pin"`
}

// handlePairingReveal exchanges a human-confirmed PIN for the actual pairing
// token held by the inviter's sidecar. The PIN is read by a human off the
// inviter's screen (returned from POST /pairing/broadcast as
// confirmation_pin) — it was never itself broadcast on the LAN. A passive
// listener that only captured the announce's fingerprint cannot complete
// this exchange: it needs the PIN, and the inviter rate-limits + burns the
// broadcast session after a small number of wrong guesses (see
// internal/lanpair.maxRevealAttempts).
func (s *Server) handlePairingReveal(w http.ResponseWriter, r *http.Request) {
	if s.lanpair == nil {
		writeError(w, http.StatusServiceUnavailable, "lan_unavailable", "LAN pairing is not available")
		return
	}
	if s.enrolled.Load() == 1 {
		writeError(w, http.StatusConflict, "already_enrolled",
			"this node is already enrolled; call POST /unpair first to re-pair")
		return
	}
	var req pairingRevealRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return
	}
	if req.Fingerprint == "" || req.PIN == "" {
		writeError(w, http.StatusBadRequest, "bad_request", "fingerprint and pin are required")
		return
	}

	token, err := s.lanpair.RequestReveal(req.Fingerprint, req.PIN, pairingRevealTimeout)
	if err != nil {
		slog.Warn("localapi: pairing reveal failed", "err", err)
		writeError(w, http.StatusUnauthorized, "reveal_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"token": token})
}

// ---------------------------------------------------------------------------
// PATCH /settings — runtime settings override
// ---------------------------------------------------------------------------

// settingsPatchRequest is the PATCH /settings body. Every field is a
// pointer so a request can update a subset of settings — an omitted field
// (nil) leaves that setting untouched, mirroring the Python sidecar's
// merge-update PATCH /settings semantics (apps/desktop/python/potluck_local/
// routers/settings.py). Only coordinator_url exists today; add more pointer
// fields here as additional runtime overrides are needed.
type settingsPatchRequest struct {
	CoordinatorURL *string `json:"coordinator_url,omitempty"`
}

// settingsResponse is returned by PATCH /settings, reflecting the settings
// state AFTER applying the patch (updated fields plus anything left
// untouched).
type settingsResponse struct {
	CoordinatorURL string `json:"coordinator_url"`
}

// handleSettingsPatch applies a runtime settings override. Currently
// supports coordinator_url only: validates it's a well-formed http/https
// URL, persists it to runtimesettings' settings.json (sited next to the WG
// key — see Config.SettingsPath), and installs it as the LIVE coordinator
// URL via ApplyCoordinatorURLFn (wired from cmd/potluck-mesh/main.go to
// update coordClient, the signal client, and ipcState, then prod a
// reconnect).
//
// Loopback/socket-only by construction: this handler, like every other
// localapi route, is only ever reachable via the Unix socket / named pipe
// this server listens on (see platformListen's same-user ACL) — there is no
// separate auth check needed here.
//
// A request with coordinator_url omitted (nil) is a no-op that just echoes
// the current value — lets a caller probe the endpoint's shape without
// requiring a full "settings object" round-trip, and leaves room for future
// settings keys to be patched independently.
func (s *Server) handleSettingsPatch(w http.ResponseWriter, r *http.Request) {
	var req settingsPatchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON body: "+err.Error())
		return
	}

	if req.CoordinatorURL == nil {
		writeJSON(w, http.StatusOK, settingsResponse{CoordinatorURL: s.state.CoordinatorURL()})
		return
	}

	normalized, err := validateCoordinatorURL(*req.CoordinatorURL)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "invalid_coordinator_url", err.Error())
		return
	}

	if s.settingsPath != "" {
		if err := runtimesettings.Save(s.settingsPath, runtimesettings.Settings{CoordinatorURL: normalized}); err != nil {
			slog.Error("localapi: settings: persist coordinator_url failed", "err", err)
			writeError(w, http.StatusInternalServerError, "persist_failed",
				"could not persist settings: "+err.Error())
			return
		}
	}

	// Apply live. Prefer the full multi-component update (coordClient +
	// signal client + state + reconnect) when wired; fall back to just
	// updating State so GET /status is at least consistent (tests / a
	// caller that only cares about the persisted value).
	if s.applyCoordinatorURLFn != nil {
		s.applyCoordinatorURLFn(normalized)
	} else {
		s.state.SetCoordinatorURL(normalized)
	}

	slog.Info("localapi: settings: coordinator_url updated", "coordinator_url", normalized)
	writeJSON(w, http.StatusOK, settingsResponse{CoordinatorURL: normalized})
}

// validateCoordinatorURL normalizes (trims whitespace + any trailing slash)
// and validates a candidate coordinator_url, mirroring
// internal/config.Load's --coordinator-url validation: must parse as a URL,
// scheme must be http or https, and a host must be present. Returns the
// normalized value on success.
func validateCoordinatorURL(raw string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
	if trimmed == "" {
		return "", fmt.Errorf("coordinator_url is required")
	}
	if !strings.HasPrefix(trimmed, "http://") && !strings.HasPrefix(trimmed, "https://") {
		return "", fmt.Errorf("coordinator_url must start with http:// or https://, got: %s", trimmed)
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return "", fmt.Errorf("coordinator_url is not a valid URL: %w", err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("coordinator_url must include a host")
	}
	return trimmed, nil
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		slog.Warn("localapi: json encode error", "err", err)
	}
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, errorBody{Error: errorDetail{Code: code, Message: message}})
}

// shortID returns a 3-hex-char suffix derived from the low bits of the current
// millisecond timestamp — low collision rate for single-machine use cases.
func shortID() string {
	return fmt.Sprintf("%03x", time.Now().UnixMilli()&0xFFF)
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}
