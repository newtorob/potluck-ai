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

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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


class EnrolledPeer(BaseModel):
    """One entry in the roster returned by enrollment."""

    node_id: uuid.UUID
    node_name: str
    mesh_ip: str
    wg_public_key: str
    last_endpoint: str | None = None
    last_seen_at: datetime | None = None
    platform: str | None = None


class EnrollResponse(BaseModel):
    """What the coordinator returns from a successful enrollment.

    Carries the newly-allocated mesh IP for the calling node + the
    roster of all OTHER nodes in the household so the new node can
    configure its WG peers immediately.
    """

    node_id: uuid.UUID
    household_id: uuid.UUID
    mesh_ip: str
    mesh_cidr: str
    peers: list[EnrolledPeer]


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
    peer_endpoint: str | None
    role: Literal["initiator", "responder"]


class SignalPong(BaseModel):
    type: Literal["pong"] = "pong"
    server_time: datetime


class SignalError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
