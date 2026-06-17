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
//	GET  /status  — current sidecar state
//	GET  /peers   — household roster with reachability
//	POST /pair    — enroll this node using a pairing token
//	POST /unpair  — wipe local keys + stop tunnel
//	WS   /events  — subscribe to the event bus
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
	"os"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	"nhooyr.io/websocket"
	"nhooyr.io/websocket/wsjson"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/lanpair"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
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
	coordinatorConnState string // "connected"|"connecting"|"disconnected"
	lastError            string
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

func (s *State) SetCoordinatorConnState(st string) {
	s.mu.Lock()
	s.coordinatorConnState = st
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

// snapshot returns a JSON-serialisable view. peerCount is supplied by the
// caller (from peerstate.Store.All()) to avoid a nested lock.
func (s *State) snapshot(peerCount int) statusResponse {
	s.mu.RLock()
	defer s.mu.RUnlock()

	resp := statusResponse{
		Version:               "0.1.0",
		MeshStatus:            string(s.meshStatus),
		CoordinatorConnection: s.coordinatorConnState,
		PeersOnlineCount:      peerCount,
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
	if s.lastError != "" {
		v := s.lastError
		resp.LastError = &v
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
	WGPrivKeyPath      string // path to detect / generate the WG private key
	SocketPath         string // "" → use platform default
	Platform           string // runtime.GOOS
	LanPair            *lanpair.Service // LAN pairing-token broadcast/listen; may be nil
}

// Server is the local IPC HTTP+WS server.
type Server struct {
	bus                *events.Bus
	ps                 *peerstate.Store
	state              *State
	peerReachFn        PeerReachabilityFunc
	isDERPRoutedFn     IsDERPRoutedFunc
	isPeerConfiguredFn IsPeerConfiguredFunc
	coordClient        *coordapi.Client
	signalRestart      SignalRestartFunc
	wgPrivKeyPath      string
	platform           string
	socketPath         string
	lanpair            *lanpair.Service

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
		bus:                cfg.Bus,
		ps:                 cfg.PeerStore,
		state:              cfg.State,
		peerReachFn:        cfg.PeerReachFn,
		isDERPRoutedFn:     cfg.IsDERPRoutedFn,
		isPeerConfiguredFn: cfg.IsPeerConfiguredFn,
		coordClient:        cfg.CoordClient,
		signalRestart:      cfg.SignalRestart,
		wgPrivKeyPath:      cfg.WGPrivKeyPath,
		platform:           cfg.Platform,
		socketPath:         sp,
		lanpair:            cfg.LanPair,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("GET /peers", s.handlePeers)
	mux.HandleFunc("POST /pair", s.handlePair)
	mux.HandleFunc("POST /unpair", s.handleUnpair)
	mux.HandleFunc("GET /events", s.handleEvents)
	mux.HandleFunc("POST /pairing/broadcast", s.handlePairingBroadcast)
	mux.HandleFunc("GET /pairing/discovered", s.handlePairingDiscovered)

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

	// If a private key already exists on disk at startup, mark as enrolled.
	if _, statErr := os.Stat(s.wgPrivKeyPath); statErr == nil {
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
	Version               string  `json:"version"`
	MeshStatus            string  `json:"mesh_status"`
	NodeID                *string `json:"node_id"`
	HouseholdID           *string `json:"household_id"`
	MeshIP                *string `json:"mesh_ip"`
	WGPublicKey           *string `json:"wg_public_key"`
	ExternalEndpoint      *string `json:"external_endpoint"`
	CoordinatorConnection string  `json:"coordinator_connection"`
	PeersOnlineCount      int     `json:"peers_online_count"`
	LastError             *string `json:"last_error"`
}

type peerResponse struct {
	NodeID           string  `json:"node_id"`
	NodeName         string  `json:"node_name"`
	MeshIP           string  `json:"mesh_ip"`
	WGPublicKey      string  `json:"wg_public_key"`
	LastEndpoint     *string `json:"last_endpoint,omitempty"`
	LastSeenAt       *string `json:"last_seen_at,omitempty"`
	Platform         *string `json:"platform,omitempty"`
	Reachable        bool    `json:"reachable"`
	ReachabilityMode *string `json:"reachability_mode"`
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
	snap := s.state.snapshot(len(s.ps.All()))
	writeJSON(w, http.StatusOK, snap)
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
		}
		if p.LastEndpoint != nil {
			pr.LastEndpoint = p.LastEndpoint
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

		// Reachability mode: DERP vs direct (only meaningful when reachable).
		if pr.Reachable {
			var mode string
			if s.isDERPRoutedFn != nil && s.isDERPRoutedFn(p.WGPublicKey) {
				mode = string(ReachDERP)
			} else {
				mode = string(ReachDirect)
			}
			pr.ReachabilityMode = &mode
		}
		// If unreachable, ReachabilityMode stays nil (JSON null).

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

	// Call the coordinator's enroll endpoint.
	platform := s.platform
	enrollReq := coordapi.EnrollRequest{
		PairingToken: req.PairingToken,
		WGPublicKey:  wgPublicKey,
		NodeName:     nodeName,
		Platform:     &platform,
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

	s.enrolled.Store(0)
	s.state.ClearEnrollment()
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
	// A discovered token is only offered if heard within this window.
	pairingDiscoveryMaxAge = 150 * time.Second
)

// handlePairingBroadcast starts broadcasting a pairing token on the LAN. Only an
// enrolled machine can invite (it is the one that issued the token); an
// unenrolled machine has no token to share.
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
	s.lanpair.StartBroadcast(body.Token, hostname, pairingBroadcastWindow)
	slog.Info("localapi: broadcasting pairing token on LAN", "window_s", int(pairingBroadcastWindow.Seconds()))
	writeJSON(w, http.StatusOK, map[string]any{
		"broadcasting":   true,
		"window_seconds": int(pairingBroadcastWindow.Seconds()),
	})
}

// handlePairingDiscovered returns a pairing token heard on the LAN, if any. Only
// meaningful on an unenrolled machine that is looking to join; an enrolled
// machine never acts on a discovered token.
func (s *Server) handlePairingDiscovered(w http.ResponseWriter, r *http.Request) {
	if s.lanpair == nil || s.enrolled.Load() == 1 {
		writeJSON(w, http.StatusOK, map[string]any{"found": false})
		return
	}
	token, hostname, ok := s.lanpair.Discovered(pairingDiscoveryMaxAge)
	if !ok {
		writeJSON(w, http.StatusOK, map[string]any{"found": false})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"found":    true,
		"token":    token,
		"hostname": hostname,
	})
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
