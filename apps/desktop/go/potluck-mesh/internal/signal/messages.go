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
type Hello struct {
	Type        string `json:"type"`
	WGPublicKey string `json:"wg_public_key"`
}

func NewHello(pubKey string) Hello {
	return Hello{Type: "hello", WGPublicKey: pubKey}
}

// EndpointUpdate tells the coordinator about this node's current external
// WireGuard endpoint (learned from STUN or interface enumeration).
// Not yet sent in the week-2 bootstrap; scaffolded for week 3.
type EndpointUpdate struct {
	Type             string `json:"type"`
	ExternalEndpoint string `json:"external_endpoint"`
}

func NewEndpointUpdate(endpoint string) EndpointUpdate {
	return EndpointUpdate{Type: "endpoint_update", ExternalEndpoint: endpoint}
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
	NodeID      string  `json:"node_id"`
	NodeName    string  `json:"node_name"`
	MeshIP      string  `json:"mesh_ip"`
	WGPublicKey string  `json:"wg_public_key"`
	LastEndpoint *string `json:"last_endpoint,omitempty"`
	Platform    *string `json:"platform,omitempty"`
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
	Type             string  `json:"type"`
	PeerNodeID       string  `json:"peer_node_id"`
	PeerWGPublicKey  string  `json:"peer_wg_public_key"`
	PeerEndpoint     *string `json:"peer_endpoint,omitempty"`
	Role             string  `json:"role"` // "initiator" | "responder"
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
	case "welcome", "roster_update", "punch_coord", "pong", "error":
		return true
	}
	return false
}

