"""Pydantic schemas for the mesh control plane.

Owns the on-the-wire shapes for:

  * POST /mesh/enroll              — peer redeems a pairing token, gets
                                     its mesh IP + initial peer roster.
  * GET  /mesh/peers               — current household roster.
  * POST /mesh/heartbeat           — node alive + last-endpoint update.

Privacy notes:

  * The mesh-layer protocol is allowed to know household membership +
    member identity — that's the entire point of household scoping.
  * The mesh layer is NEVER the place where blinded inference dispatch
    happens. That stays in `routers/jobs.py` and is enforced by the
    `JobAssignedMessage` schema (which lives in `schemas/peer.py`).
"""
from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# Cap on how many LAN candidates a node may advertise. Multi-homed hosts have a
# handful of RFC1918 addresses; more than this is almost certainly noise or
# abuse. Mirrors the client-side cap in internal/localep (Go).
MAX_LOCAL_ENDPOINTS = 4


def _parse_ip_port(value: str) -> tuple[str, int] | None:
    """Strict "<ip>:<port>" (or "[<ipv6>]:<port>") shape parser shared by every
    peer-supplied endpoint field on the wire (local_endpoints, external_endpoint,
    peer_endpoint).

    This is the defense-in-depth backstop against WireGuard UAPI config
    injection: a value like "1.1.1.1:1\\nallowed_ip=0.0.0.0/0\\n" must never
    reach the sidecar's UAPI config as an unvalidated string. Any embedded
    whitespace/newline fails immediately (before any parsing), so a payload
    can't smuggle extra UAPI lines through a value that "looks like" a valid
    endpoint at a glance. Returns (ip, port) on success, None otherwise —
    never raises.
    """
    if not isinstance(value, str) or not value:
        return None
    if any(ch.isspace() for ch in value):
        return None
    if value.startswith("["):
        # Bracketed IPv6 form: "[<ipv6>]:<port>" (matches Go's net.JoinHostPort).
        close = value.find("]")
        if close == -1 or value[close + 1 : close + 2] != ":":
            return None
        host = value[1:close]
        port = value[close + 2 :]
    else:
        host, sep, port = value.rpartition(":")
        if not sep or not host:
            return None
    if not port.isdigit():
        return None
    port_num = int(port)
    if port_num < 1 or port_num > 65535:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return str(ip), port_num


def _is_private_lan_endpoint(value: str) -> bool:
    """Server-side defense in depth for LAN direct-path candidates.

    Returns True only when `value` is "<ip>:<port>" where ip is an IPv4 RFC1918
    address (10/8, 172.16/12, 192.168/16) and port is 1..65535. Loopback,
    link-local, and the tailnet/CGNAT range (100.64.0.0/10) are rejected so a
    Tailscale address can never be redistributed as a physical-LAN endpoint —
    even if a malicious or buggy client claims one.
    """
    parsed = _parse_ip_port(value)
    if parsed is None:
        return False
    host, _port_num = parsed
    ip = ipaddress.ip_address(host)
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_link_local:
        return False
    # 100.64.0.0/10 — tailnet / CGNAT. is_private would say True, so check first.
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return ip.is_private


def safe_endpoint_or_none(value: str | None) -> str | None:
    """Coerce a DB-stored endpoint value (e.g. MeshNode.last_endpoint) to
    None if it isn't well-formed ip:port. Rows written before this
    validation shipped could in principle hold a legacy malformed value;
    rather than let SignalPunchCoord's field_validator raise (which would
    kill the whole signaling connection over one peer's stale row), callers
    that relay a stored endpoint onward should pass it through this first."""
    if value is None or not _is_well_formed_endpoint(value):
        return None
    return value


def _is_well_formed_endpoint(value: str) -> bool:
    """Format-only check for endpoint fields that carry PUBLIC (or NAT'd)
    addresses — external_endpoint / peer_endpoint. Unlike
    `_is_private_lan_endpoint`, this does NOT filter by address scope (a
    public IP is exactly what's expected here); it only rejects malformed
    shapes, embedded whitespace/newlines, and out-of-range ports. That's
    enough to stop wire-protocol injection into the sidecar's WireGuard UAPI
    config while still accepting any legitimate STUN-discovered endpoint."""
    return _parse_ip_port(value) is not None


def _validate_endpoint_field(v: object) -> str | None:
    """Shared field_validator body for optional "ip:port" endpoint fields.
    None is always allowed (endpoint not yet known). Anything else must be a
    well-formed ip:port string, or Pydantic rejects the request — this is
    intentionally a hard validation error, not a silent truncation/drop, so a
    malformed value never quietly flows through to a peer."""
    if v is None:
        return None
    if not isinstance(v, str) or not _is_well_formed_endpoint(v):
        raise ValueError(
            "must be a well-formed 'ip:port' (or '[ipv6]:port') address with "
            "no whitespace/newlines"
        )
    return v


def _filter_local_endpoints(values: list[str]) -> list[str]:
    """Drop any entry that isn't a private ip:port, dedup (preserving order),
    and cap the list length. Applied server-side to inbound updates."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        if not _is_private_lan_endpoint(v):
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= MAX_LOCAL_ENDPOINTS:
            break
    return out


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    """Body of POST /mesh/enroll.

    A node that has been given a pairing token presents it here along
    with its locally generated WireGuard public key + node attestation
    fields. The coordinator verifies the token, allocates a mesh IP
    from the household's CIDR, and returns the roster.

    The WG private key NEVER leaves the node. Identity rotation is
    handled by a separate endpoint (post-launch).
    """

    pairing_token: str = Field(..., min_length=16, max_length=64)
    wg_public_key: str = Field(..., min_length=32, max_length=128)
    node_name: str = Field(..., min_length=1, max_length=128)
    platform: str | None = Field(default=None, max_length=32)
    # OPTIONAL Ed25519 public key (base64url, no padding) used ONLY for the
    # DERP-token proof-of-possession challenge (POST /mesh/derp-token) — see
    # `MeshNode.signing_public_key`. Absent for any mesh client that hasn't
    # picked up PoP support yet (back-compat: old clients omit this field and
    # enroll exactly as before). Format-validated only where it's actually
    # used (issue_derp_token), not here, so a malformed value never blocks
    # enrollment itself.
    signing_public_key: str | None = Field(default=None, min_length=32, max_length=64)


class EnrolledPeer(BaseModel):
    """One entry in the roster returned by enrollment.

    `local_endpoints` carries the peer's private-LAN "ip:port" candidates
    (RFC1918 only) so same-LAN peers can later attempt a direct connection
    instead of relaying through DERP. Phase 0/1 distributes the list but no
    routing decision uses it yet. The list is held in-memory only (last value
    reported over the signal WS); it is NOT persisted to the DB.
    """

    node_id: uuid.UUID
    node_name: str
    mesh_ip: str
    wg_public_key: str
    last_endpoint: str | None = None
    local_endpoints: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    platform: str | None = None


class EnrollResponse(BaseModel):
    """What the coordinator returns from a successful enrollment.

    Carries the newly-allocated mesh IP for the calling node + the
    roster of all OTHER nodes in the household so the new node can
    configure its WG peers immediately.

    `node_token` (ADDITIVE) is a fresh, high-entropy per-node bearer
    credential — urlsafe-base64, 32 raw bytes (256 bits). The coordinator
    stores only its SHA-256 hash (`MeshNode.node_token_hash`); this is the
    ONLY time the raw value is exposed, exactly like `pairing_token` in
    `PairingTokenIssueResponse`. Present it as `Authorization: Bearer
    <node_token>` on every subsequent machine call (POST /mesh/heartbeat,
    GET /mesh/peers, POST /mesh/derp-token, DELETE /mesh/nodes/{id}, and the
    `/mesh/signal` WS upgrade) instead of the machine-wide
    `X-Potluck-Coordinator-Token`. Old mesh clients that don't read this
    field are unaffected — they keep using the coordinator token exactly as
    before (back-compat; see `middleware/node_auth.py`).
    """

    node_id: uuid.UUID
    household_id: uuid.UUID
    mesh_ip: str
    mesh_cidr: str
    peers: list[EnrolledPeer]
    node_token: str


# ---------------------------------------------------------------------------
# Roster + heartbeat
# ---------------------------------------------------------------------------


class HeartbeatRequest(BaseModel):
    """Body of POST /mesh/heartbeat.

    Sidecar reports its currently-discovered external endpoint (from
    STUN) so the coordinator can broadcast it to other household peers.
    """

    node_id: uuid.UUID
    wg_public_key: str
    external_endpoint: str | None = Field(default=None, max_length=64)

    @field_validator("external_endpoint")
    @classmethod
    def _validate_external_endpoint(cls, v: str | None) -> str | None:
        return _validate_endpoint_field(v)


class HeartbeatResponse(BaseModel):
    accepted: bool = True
    server_time: datetime


# ---------------------------------------------------------------------------
# Household + pairing-token issuance
# ---------------------------------------------------------------------------


class HouseholdCreateRequest(BaseModel):
    """Body of POST /mesh/households.

    v0/dev: name is the only client-supplied field. Household is bound
    to the authenticated member (stub-id in v0). Mesh CIDR is allocated
    by the coordinator from the global pool — clients don't pick it.
    """

    name: str = Field(default="My household", min_length=1, max_length=128)


class HouseholdResponse(BaseModel):
    id: uuid.UUID
    owner_member_id: uuid.UUID
    name: str
    mesh_cidr: str
    created_at: datetime


class PairingTokenIssueResponse(BaseModel):
    """Returned by POST /mesh/pairing-tokens.

    `token` is opaque base32 (~110 bits entropy). The coordinator stores
    a copy; this is the only time the plaintext is exposed.
    """

    token: str
    household_id: uuid.UUID
    expires_at: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# Signaling WebSocket message protocol (/mesh/signal)
# ---------------------------------------------------------------------------
#
# All signal messages use a tagged-union shape: the `type` field carries
# the discriminator, and Pydantic parses into the appropriate variant.
# The contract is bidirectional — server and client both send + receive.
#
# Client → Server messages: SignalHello, SignalEndpointUpdate,
#                            SignalRequestPunch, SignalPing
# Server → Client messages: SignalWelcome, SignalRosterUpdate,
#                            SignalPunchCoord, SignalPong, SignalError
#
# The first message a client sends MUST be SignalHello with its
# wg_public_key. If it's not registered in mesh_nodes, server closes
# with code 1008. Otherwise server replies with SignalWelcome carrying
# the current household roster.
# ---------------------------------------------------------------------------

from typing import Literal


# ---- Client → Server ---- #


class SignalHello(BaseModel):
    type: Literal["hello"] = "hello"
    wg_public_key: str = Field(..., min_length=32, max_length=128)


class SignalEndpointUpdate(BaseModel):
    type: Literal["endpoint_update"] = "endpoint_update"
    external_endpoint: str = Field(..., max_length=64)  # "ip:port"
    # Private-LAN "ip:port" candidates for same-LAN direct connections. The
    # validator below re-applies the RFC1918 filter server-side (defense in
    # depth): non-private entries are dropped and the list is capped, so a
    # client cannot get a tailnet/public address redistributed to peers.
    local_endpoints: list[str] = Field(default_factory=list)

    @field_validator("external_endpoint")
    @classmethod
    def _validate_external_endpoint(cls, v: str) -> str:
        # external_endpoint is required (not Optional) here, but reuse the
        # shared None-tolerant validator — a non-string/malformed value still
        # raises, `None` simply can't occur once Pydantic's own required-field
        # check has passed.
        validated = _validate_endpoint_field(v)
        if validated is None:
            raise ValueError("external_endpoint is required")
        return validated

    @field_validator("local_endpoints", mode="before")
    @classmethod
    def _sanitize_local_endpoints(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return _filter_local_endpoints(v)


class SignalRequestPunch(BaseModel):
    """Peer wants to coordinate a simultaneous-send hole-punch to a
    specific household peer. Coordinator forwards `SignalPunchCoord`
    to both sides with the other's endpoint."""

    type: Literal["request_punch"] = "request_punch"
    target_node_id: uuid.UUID


class SignalPing(BaseModel):
    type: Literal["ping"] = "ping"


# ---- Server → Client ---- #


class SignalWelcome(BaseModel):
    """Sent immediately after a successful hello."""

    type: Literal["welcome"] = "welcome"
    node_id: uuid.UUID
    household_id: uuid.UUID
    mesh_ip: str
    roster: list[EnrolledPeer]


class SignalRosterUpdate(BaseModel):
    """Broadcast to all open household connections when a peer joins,
    leaves, or updates its endpoint."""

    type: Literal["roster_update"] = "roster_update"
    added: list[EnrolledPeer] = Field(default_factory=list)
    updated: list[EnrolledPeer] = Field(default_factory=list)
    removed: list[uuid.UUID] = Field(default_factory=list)


class SignalPunchCoord(BaseModel):
    """Server delivers this to both peers participating in a hole-punch.
    `role='initiator'` means "you asked for it"; `role='responder'`
    means "someone wants to reach you, send simultaneously."""

    type: Literal["punch_coord"] = "punch_coord"
    peer_node_id: uuid.UUID
    peer_wg_public_key: str
    # Previously had NO length cap and no format validation at all (this field
    # is server-constructed FROM a stored `last_endpoint`, but that value
    # originated as untrusted peer input via heartbeat/endpoint_update — so it
    # needs the same ip:port validation at the point it's relayed onward).
    peer_endpoint: str | None = Field(default=None, max_length=64)
    role: Literal["initiator", "responder"]

    @field_validator("peer_endpoint")
    @classmethod
    def _validate_peer_endpoint(cls, v: str | None) -> str | None:
        return _validate_endpoint_field(v)


class SignalPong(BaseModel):
    type: Literal["pong"] = "pong"
    server_time: datetime


class SignalError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


# ---------------------------------------------------------------------------
# DERP token issuance (/mesh/derp-token)
# ---------------------------------------------------------------------------
#
# A mesh node calls POST /mesh/derp-token to obtain a short-lived JWT it
# can use to authenticate with the DERP relay. The JWT is signed with a
# symmetric key (POTLUCK_DERP_JWT_KEY) shared between the coordinator and
# the relay. The relay validates the JWT on connect and only accepts
# connections for pubkeys + regions that match the claims.
#
# Privacy notes:
#   * JWT claims carry wg_pubkey + region + exp/iat only.
#   * No member_id, no household_id, no node_id in the JWT — the relay
#     should never know "who" the user is, only "which WG key".
#   * The coordinator logs the truncated pubkey (first 8 chars), not the
#     full token or claims.
# ---------------------------------------------------------------------------


class DERPTokenRequest(BaseModel):
    """Body of POST /mesh/derp-token.

    Caller identifies itself by its WG public key. The coordinator looks
    up the corresponding mesh_node to confirm it is enrolled.

    v0 auth model: possession of the WG pubkey is the ONLY credential
    (`wg_public_key` is not secret — it's exchanged with peers anyway — so
    this alone lets anyone who has observed a node's pubkey mint a token
    AS that node and hijack its DERP session).

    v0.5 OPTIONAL proof-of-possession hardening (back-compat, additive):
    `nonce` + `signature` let the caller prove it holds the private key for
    a separately-registered `MeshNode.signing_public_key` (an Ed25519 key;
    WG keys are Curve25519 and are not directly usable for signature
    verification — see `models/mesh_node.py`). Both fields are OPTIONAL and
    default to None so existing mesh clients (incl. the current Go
    `coordapi` client) that don't send them keep working exactly as before;
    the coordinator only verifies when both are present, and logs a warning
    when they're absent. See `routers/mesh.py::issue_derp_token` for the
    verification + the `POTLUCK_DERP_POP_REQUIRED` flag that will eventually
    make this mandatory once the Go client signs by default.

    `nonce` is a caller-generated, timestamp-based value (`<node's current
    UTC unix timestamp>`) rather than a coordinator-issued challenge, so PoP
    doesn't require an extra round trip; the coordinator rejects a nonce
    outside a small clock-skew window to bound replay. `signature` is the
    base64url Ed25519 signature (no padding) of
    `f"derp-token:{wg_public_key}:{region}:{nonce}"` (UTF-8 encoded),
    produced with the private key matching `MeshNode.signing_public_key`.

    No member_id or node_id in this schema — the relay must never learn
    the calling user's identity, only their WG pubkey and requested region.
    """

    wg_public_key: str = Field(..., min_length=32, max_length=128)
    region: str = Field(default="ord", min_length=1, max_length=32)
    nonce: str | None = Field(default=None, min_length=1, max_length=32)
    signature: str | None = Field(default=None, min_length=1, max_length=128)


class DERPTokenResponse(BaseModel):
    """Returned by a successful POST /mesh/derp-token.

    `token` is a compact HS256 JWT. The relay validates this on connect.
    The `expires_at` is ISO 8601 — the client SHOULD refresh before this
    time (the DERP client refreshes every 50 minutes for a 1-hour token).

    No inference-layer identity in this response.
    """

    token: str
    expires_at: str  # ISO 8601 datetime string
    region: str
