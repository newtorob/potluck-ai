// Package signal implements the client side of the Potluck coordinator's
// /mesh/signal WebSocket protocol.
//
// Wire shapes mirror the Python Pydantic schemas in
// services/coordinator/coordinator/schemas/mesh.py exactly.
// Every field name matches the JSON produced by model.model_dump(mode="json").
//
// Privacy: this package handles mesh-layer metadata only (node IDs, mesh IPs,
// WG public keys, endpoints). It never sees prompt or response content.
package signal

import (
	"time"
)

// ---- client → server ---- //

// Hello is the first message a client sends after connecting.
// The server closes with code 1008 if the wg_public_key is not in mesh_nodes.
//
// Features lists optional capabilities of this sidecar (see
// FeatureCircleLinkV1). The coordinator never sends a message type to a sidecar
// that did not name it, and an old coordinator ignores the key.
type Hello struct {
	Type        string   `json:"type"`
	WGPublicKey string   `json:"wg_public_key"`
	Features    []string `json:"features,omitempty"`
}

func NewHello(pubKey string) Hello {
	return Hello{Type: "hello", WGPublicKey: pubKey}
}

// NewHelloWithFeatures is NewHello plus the capability list.
func NewHelloWithFeatures(pubKey string, features []string) Hello {
	h := NewHello(pubKey)
	h.Features = append([]string(nil), features...)
	return h
}

// EndpointUpdate tells the coordinator about this node's current external
// WireGuard endpoint (learned from STUN or interface enumeration).
//
// LocalEndpoints carries this node's private-LAN "ip:port" candidates (RFC1918
// only; never a tailnet address) so peers on the same LAN can later attempt a
// direct connection instead of relaying through DERP. As of Phase 0/1 this is
// additive plumbing only: the coordinator stores + redistributes the list but
// no routing decision uses it yet.
type EndpointUpdate struct {
	Type             string   `json:"type"`
	ExternalEndpoint string   `json:"external_endpoint"`
	LocalEndpoints   []string `json:"local_endpoints,omitempty"`
}

func NewEndpointUpdate(endpoint string, local []string) EndpointUpdate {
	return EndpointUpdate{
		Type:             "endpoint_update",
		ExternalEndpoint: endpoint,
		LocalEndpoints:   local,
	}
}

// Ping is a keepalive sent every 30 seconds.
type Ping struct {
	Type string `json:"type"`
}

func NewPing() Ping { return Ping{Type: "ping"} }

// RequestPunch asks the coordinator to broker a simultaneous-send hole-punch
// with the specified peer. The coordinator responds to BOTH parties with a
// PunchCoord message carrying the other's endpoint and a role discriminator.
//
// Wire shape mirrors schemas/mesh.py:SignalRequestPunch exactly.
type RequestPunch struct {
	Type         string `json:"type"`
	TargetNodeID string `json:"target_node_id"` // UUID string
}

func NewRequestPunch(targetNodeID string) RequestPunch {
	return RequestPunch{Type: "request_punch", TargetNodeID: targetNodeID}
}

// ---- server → client ---- //

// RosterPeer mirrors schemas/mesh.py:EnrolledPeer.
// Only mesh-layer fields are present; no inference content, no member_id.
type RosterPeer struct {
	NodeID         string   `json:"node_id"`
	NodeName       string   `json:"node_name"`
	MeshIP         string   `json:"mesh_ip"`
	WGPublicKey    string   `json:"wg_public_key"`
	LastEndpoint   *string  `json:"last_endpoint,omitempty"`
	LocalEndpoints []string `json:"local_endpoints,omitempty"`
	Platform       *string  `json:"platform,omitempty"`
}

// Welcome is the first message the server sends after a valid Hello.
// It carries the calling node's assigned identity + the current household roster.
type Welcome struct {
	Type        string       `json:"type"`
	NodeID      string       `json:"node_id"`
	HouseholdID string       `json:"household_id"`
	MeshIP      string       `json:"mesh_ip"`
	Roster      []RosterPeer `json:"roster"`
}

// RosterUpdate is broadcast to all household peers when a node joins, leaves,
// or updates its endpoint.
type RosterUpdate struct {
	Type    string       `json:"type"`
	Added   []RosterPeer `json:"added"`
	Updated []RosterPeer `json:"updated"`
	Removed []string     `json:"removed"` // list of node_id (UUID strings)
}

// PunchCoord is delivered by the coordinator to both parties in a hole-punch.
// Scaffolded here for later weeks; not acted on in week 2.
type PunchCoord struct {
	Type            string  `json:"type"`
	PeerNodeID      string  `json:"peer_node_id"`
	PeerWGPublicKey string  `json:"peer_wg_public_key"`
	PeerEndpoint    *string `json:"peer_endpoint,omitempty"`
	Role            string  `json:"role"` // "initiator" | "responder"
}

// Pong is the server's reply to a Ping.
type Pong struct {
	Type       string    `json:"type"`
	ServerTime time.Time `json:"server_time"`
}

// ServerError carries a categorical error code + human-readable message.
// The connection may or may not be closed after this — the client treats
// it as advisory logging unless the WS closes.
type ServerError struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Envelope is used for initial type discrimination: read the "type" field,
// then re-decode the raw bytes into the appropriate concrete type.
type Envelope struct {
	Type string `json:"type"`
}

// knownType returns true for server-sent types the client knows how to handle.
func knownType(t string) bool {
	switch t {
	case "welcome", "roster_update", "punch_coord", "pong", "error",
		"circle_peer_add", "circle_peer_extend", "circle_peer_remove":
		return true
	}
	return false
}
