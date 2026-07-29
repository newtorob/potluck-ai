// Package coordapi provides a typed HTTP client for the Potluck coordinator's
// mesh enrollment + roster endpoints.
//
// This is intentionally dependency-free (stdlib net/http only, plus sync for
// the node-token guard). The package covers the endpoints the sidecar calls
// directly outside the persistent signal WS:
//
//   - POST /mesh/enroll — redeem a pairing token, get mesh IP + roster + a
//     fresh per-node bearer credential (EnrollResponse.NodeToken).
//   - GET  /mesh/peers  — pull the authoritative household roster (the
//     roster-reconcile safety net for signal-WS broadcasts missed while
//     disconnected).
//
// The pairing-token issuance endpoint (POST /mesh/pairing-tokens) is NOT here
// because that call is made from Electron directly (the desktop app drives the
// "Add another machine" wizard), not from the sidecar.
//
// Auth (matches coordinator/middleware/node_auth.py byte-for-byte): every
// request attaches `Authorization: Bearer <node_token>` whenever one is
// loaded (SetNodeToken/NodeToken — the primary credential, node-scoped
// server-side) and the legacy `X-Potluck-Coordinator-Token` whenever
// CoordinatorToken is configured (belt-and-suspenders / dev-fleet
// back-compat, never required). POST /mesh/enroll is the one call that also
// accepts a valid pairing_token in the body as sufficient auth on its own —
// see the coordinator's enroll() docstring — so enrollment works with no
// token at all attached by this client, node_token included (it doesn't
// exist yet on a fresh install).
//
// Privacy: never logs pairing tokens, node tokens, WG private keys, or full
// enrollment response bodies. Coordinator errors are surfaced by HTTP status
// code + body fragment only.
package coordapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// defaultHTTPTimeout is used when the caller provides a nil http.Client.
const defaultHTTPTimeout = 30 * time.Second

// Client is a typed wrapper around coordinator mesh endpoints.
// BaseURL (via the BaseURL()/SetBaseURL() accessors) must not have a
// trailing slash.
type Client struct {
	HTTPClient *http.Client
	// CoordinatorToken, when set, is sent as X-Potluck-Coordinator-Token on
	// every coordinator request — the legacy machine-wide shared secret.
	// ADDITIVE / back-compat only (dev fleet): the per-node bearer credential
	// below (nodeToken) is the primary credential and is NEVER required to be
	// paired with this. Empty = not configured; coordinator token omitted
	// from every request but that's fine as long as a node_token is set, or
	// the coordinator itself runs in dev passthrough.
	CoordinatorToken string

	// baseURLMu guards baseURL. Same rationale as nodeTokenMu below: a
	// runtime coordinator-URL override (PATCH /settings on the local IPC
	// socket — see internal/localapi's handleSettingsPatch, wired via
	// cmd/potluck-mesh/main.go's applyCoordinatorURL) calls SetBaseURL live,
	// which can race an in-flight Enroll/ListPeers call from the
	// roster-reconcile loop.
	baseURLMu sync.RWMutex
	baseURL   string

	// nodeTokenMu guards nodeToken. It is set once at startup (loaded from
	// disk by main.go if this node was already enrolled) and again after a
	// fresh POST /mesh/enroll mints a new one (internal/localapi's
	// handlePair) — that second Set can race an in-flight ListPeers call
	// from the roster-reconcile loop, hence the lock rather than a bare
	// string field.
	nodeTokenMu sync.RWMutex
	nodeToken   string
}

// New creates a Client with the given base URL and a default 30-second timeout.
func New(baseURL string) *Client {
	return &Client{
		baseURL: baseURL,
		HTTPClient: &http.Client{
			Timeout: defaultHTTPTimeout,
		},
	}
}

// BaseURL returns the currently configured coordinator base URL. Safe to
// call concurrently with in-flight requests and SetBaseURL. Also used as the
// LIVE coordinator-URL source for the DERP JWT fetcher (see
// cmd/potluck-mesh/main.go's buildDERPBind/makeDERPJWTFetcher), mirroring how
// NodeToken is the shared live source for the bearer credential.
func (c *Client) BaseURL() string {
	c.baseURLMu.RLock()
	defer c.baseURLMu.RUnlock()
	return c.baseURL
}

// SetBaseURL updates the coordinator base URL used by every subsequent
// request (Enroll, ListPeers, and anything reading it live via BaseURL()).
// Safe to call concurrently with in-flight requests — mirrors
// SetNodeToken's contract. Called from a runtime coordinator-URL override
// (PATCH /settings); never required for normal operation (the URL passed to
// New at startup is otherwise the only value ever in play).
func (c *Client) SetBaseURL(baseURL string) {
	c.baseURLMu.Lock()
	c.baseURL = baseURL
	c.baseURLMu.Unlock()
}

// SetNodeToken installs the per-node bearer credential (`Authorization:
// Bearer <token>`) used on every subsequent request. Safe to call
// concurrently with in-flight requests (e.g. the roster-reconcile loop's
// ListPeers). Pass "" to clear it (called from /unpair).
func (c *Client) SetNodeToken(token string) {
	c.nodeTokenMu.Lock()
	c.nodeToken = token
	c.nodeTokenMu.Unlock()
}

// NodeToken returns the currently installed per-node bearer credential, or
// "" if none is available (not yet enrolled, or enrolled before node_token
// support shipped and never re-paired since). Also used as the live token
// source for the signal WS dialer and the DERP-token fetcher — see
// cmd/potluck-mesh/main.go, which passes this method value through to both
// so a freshly-minted token (set here by handlePair) reaches every
// coordinator-facing caller without separate plumbing.
func (c *Client) NodeToken() string {
	c.nodeTokenMu.RLock()
	defer c.nodeTokenMu.RUnlock()
	return c.nodeToken
}

// attachAuth sets the auth headers on an outbound coordinator request. The
// per-node bearer credential is the primary credential and is attached
// whenever one is loaded; the legacy X-Potluck-Coordinator-Token is attached
// whenever configured, unconditionally alongside it (belt-and-suspenders /
// dev-fleet back-compat — never required, never mutually exclusive with the
// bearer). Mirrors coordinator/middleware/node_auth.py's
// resolve_node_from_token: either credential is independently sufficient
// server-side, so sending both when both happen to be available is harmless.
func (c *Client) attachAuth(req *http.Request) {
	if nt := c.NodeToken(); nt != "" {
		req.Header.Set("Authorization", "Bearer "+nt)
	}
	if c.CoordinatorToken != "" {
		req.Header.Set("X-Potluck-Coordinator-Token", c.CoordinatorToken)
	}
}

// EnrollRequest mirrors coordinator/schemas/mesh.py:EnrollRequest.
// The WG private key NEVER appears here — only the derived public key.
type EnrollRequest struct {
	PairingToken string  `json:"pairing_token"`
	WGPublicKey  string  `json:"wg_public_key"`
	NodeName     string  `json:"node_name"`
	Platform     *string `json:"platform,omitempty"`
	// SigningPublicKey is the OPTIONAL Ed25519 public key (base64url, no
	// padding — see internal/signingkey) used ONLY for the DERP-token
	// proof-of-possession challenge. Mirrors
	// coordinator/schemas/mesh.py:EnrollRequest.signing_public_key exactly:
	// omitted (nil) is back-compat with any coordinator that predates PoP
	// hardening, and is itself perfectly safe — the coordinator's DERP-token
	// endpoint only verifies a signature when one is actually sent.
	SigningPublicKey *string `json:"signing_public_key,omitempty"`
}

// EnrolledPeer mirrors coordinator/schemas/mesh.py:EnrolledPeer.
// Contains only mesh-layer metadata; no inference content.
type EnrolledPeer struct {
	NodeID         string   `json:"node_id"` // UUID string
	NodeName       string   `json:"node_name"`
	MeshIP         string   `json:"mesh_ip"`
	WGPublicKey    string   `json:"wg_public_key"`
	LastEndpoint   *string  `json:"last_endpoint,omitempty"`
	LocalEndpoints []string `json:"local_endpoints,omitempty"` // private-LAN candidates
	LastSeenAt     *string  `json:"last_seen_at,omitempty"`    // RFC3339 or null
	Platform       *string  `json:"platform,omitempty"`
}

// EnrollResponse mirrors coordinator/schemas/mesh.py:EnrollResponse.
type EnrollResponse struct {
	NodeID      string         `json:"node_id"`      // UUID string
	HouseholdID string         `json:"household_id"` // UUID string
	MeshIP      string         `json:"mesh_ip"`
	MeshCIDR    string         `json:"mesh_cidr"`
	Peers       []EnrolledPeer `json:"peers"`
	// NodeToken is the freshly-minted per-node bearer credential — urlsafe
	// base64 of 32 raw bytes (~43 chars, no padding), shown ONCE. Mirrors
	// coordinator/schemas/mesh.py:EnrollResponse.node_token exactly. The
	// caller (internal/localapi's handlePair) MUST persist this immediately
	// (internal/nodetoken.Save) and install it as the live credential
	// (Client.SetNodeToken) — there is no way to retrieve it again later.
	NodeToken string `json:"node_token"`
}

// CoordinatorError represents a structured error returned by the coordinator.
// The body comes from a JSON `{"detail": "..."}` (FastAPI default) or
// `{"error": {"code": "...", "message": "..."}}` shape.
type CoordinatorError struct {
	StatusCode int
	Body       string
}

func (e *CoordinatorError) Error() string {
	return fmt.Sprintf("coordinator returned HTTP %d: %s", e.StatusCode, e.Body)
}

// Enroll calls POST /mesh/enroll with the given request and returns the parsed
// EnrollResponse on 201, or a *CoordinatorError on any other status code.
//
// The context is threaded through the HTTP request so callers can cancel or
// time-box the call independently of the Client.HTTPClient.Timeout.
func (c *Client) Enroll(ctx context.Context, req EnrollRequest) (*EnrollResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("coordapi: marshal enroll request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.BaseURL()+"/mesh/enroll",
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("coordapi: build enroll request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")
	// Enroll is the one call the coordinator will accept on the pairing
	// token alone (see coordinator/routers/mesh.py::enroll) — a node_token
	// never exists yet at this point in a fresh enrollment (attachAuth is a
	// no-op for it then), but attach whatever's loaded regardless: a re-pair
	// after /unpair always clears both credentials first, and the legacy
	// coordinator token (when configured) still works as this call's
	// back-compat fallback auth exactly as before.
	c.attachAuth(httpReq)

	cl := c.HTTPClient
	if cl == nil {
		cl = &http.Client{Timeout: defaultHTTPTimeout}
	}

	resp, err := cl.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("coordapi: enroll HTTP: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if err != nil {
		return nil, fmt.Errorf("coordapi: read enroll response body: %w", err)
	}

	if resp.StatusCode != http.StatusCreated {
		// Surface the raw body (truncated) for the IPC caller to forward upstream.
		snippet := string(respBody)
		if len(snippet) > 512 {
			snippet = snippet[:512] + "…"
		}
		return nil, &CoordinatorError{StatusCode: resp.StatusCode, Body: snippet}
	}

	var result EnrollResponse
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("coordapi: decode enroll response: %w", err)
	}
	return &result, nil
}

// ListPeers calls GET /mesh/peers?wg_public_key=... and returns the caller's
// full household roster (which INCLUDES the caller's own node) on 200, or a
// *CoordinatorError on any other status. Gated by the coordinator's
// resolve_node_from_token dependency (mirrors POST /mesh/heartbeat + the
// signal WS): the per-node bearer credential (attachAuth, when loaded) or the
// legacy coordinator token authenticate the call, and wgPublicKey must belong
// to whichever node the bearer resolves to or the coordinator 403s.
//
// Used by the sidecar's roster-reconcile loop as a safety net for roster_update
// broadcasts missed over the signal WS.
func (c *Client) ListPeers(ctx context.Context, wgPublicKey string) ([]EnrolledPeer, error) {
	endpoint := c.BaseURL() + "/mesh/peers?wg_public_key=" + url.QueryEscape(wgPublicKey)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("coordapi: build list-peers request: %w", err)
	}
	httpReq.Header.Set("Accept", "application/json")
	c.attachAuth(httpReq)

	cl := c.HTTPClient
	if cl == nil {
		cl = &http.Client{Timeout: defaultHTTPTimeout}
	}

	resp, err := cl.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("coordapi: list-peers HTTP: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return nil, fmt.Errorf("coordapi: read list-peers response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		snippet := string(respBody)
		if len(snippet) > 512 {
			snippet = snippet[:512] + "…"
		}
		return nil, &CoordinatorError{StatusCode: resp.StatusCode, Body: snippet}
	}

	var peers []EnrolledPeer
	if err := json.Unmarshal(respBody, &peers); err != nil {
		return nil, fmt.Errorf("coordapi: decode list-peers response: %w", err)
	}
	return peers, nil
}
