// Package coordapi provides a typed HTTP client for the Potluck coordinator's
// mesh enrollment endpoint.
//
// This is intentionally dependency-free (stdlib net/http only). The package
// covers only the endpoints the sidecar calls directly during the /pair flow:
//
//   - POST /mesh/enroll — redeem a pairing token, get mesh IP + roster.
//
// The pairing-token issuance endpoint (POST /mesh/pairing-tokens) is NOT here
// because that call is made from Electron directly (the desktop app drives the
// "Add another machine" wizard), not from the sidecar.
//
// Privacy: never logs pairing tokens, WG private keys, or full enrollment
// response bodies. Coordinator errors are surfaced by HTTP status code + body
// fragment only.
package coordapi

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// defaultHTTPTimeout is used when the caller provides a nil http.Client.
const defaultHTTPTimeout = 30 * time.Second

// Client is a typed wrapper around coordinator mesh endpoints.
// BaseURL must not have a trailing slash.
type Client struct {
	BaseURL    string
	HTTPClient *http.Client
}

// New creates a Client with the given base URL and a default 30-second timeout.
func New(baseURL string) *Client {
	return &Client{
		BaseURL: baseURL,
		HTTPClient: &http.Client{
			Timeout: defaultHTTPTimeout,
		},
	}
}

// EnrollRequest mirrors coordinator/schemas/mesh.py:EnrollRequest.
// The WG private key NEVER appears here — only the derived public key.
type EnrollRequest struct {
	PairingToken string  `json:"pairing_token"`
	WGPublicKey  string  `json:"wg_public_key"`
	NodeName     string  `json:"node_name"`
	Platform     *string `json:"platform,omitempty"`
}

// EnrolledPeer mirrors coordinator/schemas/mesh.py:EnrolledPeer.
// Contains only mesh-layer metadata; no inference content.
type EnrolledPeer struct {
	NodeID       string  `json:"node_id"`       // UUID string
	NodeName     string  `json:"node_name"`
	MeshIP       string  `json:"mesh_ip"`
	WGPublicKey  string  `json:"wg_public_key"`
	LastEndpoint *string `json:"last_endpoint,omitempty"`
	LastSeenAt   *string `json:"last_seen_at,omitempty"` // RFC3339 or null
	Platform     *string `json:"platform,omitempty"`
}

// EnrollResponse mirrors coordinator/schemas/mesh.py:EnrollResponse.
type EnrollResponse struct {
	NodeID      string         `json:"node_id"`      // UUID string
	HouseholdID string         `json:"household_id"` // UUID string
	MeshIP      string         `json:"mesh_ip"`
	MeshCIDR    string         `json:"mesh_cidr"`
	Peers       []EnrolledPeer `json:"peers"`
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
		c.BaseURL+"/mesh/enroll",
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("coordapi: build enroll request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")

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
