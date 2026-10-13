"""Mesh control-plane endpoints.

This is the coordinator surface area for Potluck's own WireGuard mesh
(per `docs/phase-g/mesh-design.md`). Owns:

  * POST /mesh/households - create the calling member's household
                                  (idempotent for the dev-stub member).
  * POST /mesh/pairing-tokens - issue a one-time enrollment token for
                                  the member's household.
  * POST /mesh/enroll - redeem a pairing token, get a mesh IP
                                  + initial peer roster.
  * GET  /mesh/peers - current household roster for an
                                  enrolled node (auth via wg_public_key).
  * POST /mesh/heartbeat - node alive + last-endpoint update.
  * WS   /mesh/signal - long-lived signaling channel. Carries
                                  endpoint updates + roster broadcasts +
                                  hole-punch coordination between peers.

Privacy posture:
  * The coordinator knows member identity + household membership. That's
    intentional - household scoping requires it.
  * The coordinator never carries this identity through to a contributor
    peer in a JobAssignedMessage. That invariant lives in `routers/jobs.py`
    and is tested at the schema level (test_privacy_invariants.py).
  * Mesh enrollment payload contains only mesh-layer data: pairing token,
    WG pubkey, node name, platform string. No member_id field is needed
    at this endpoint because the pairing token already encodes household
    membership.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import get_settings
from coordinator.crypto import KeyDecodeError, decode_ed25519_public_key, verify_ed25519_signature
from coordinator.db.database import get_db, get_session_factory
from coordinator.models.household import Household
from coordinator.models.mesh_node import MeshNode
from coordinator.models.pairing import PairingToken
from coordinator.schemas.mesh import (
    CurrentHouseholdResponse,
    DERPTokenRequest,
    DERPTokenResponse,
    EnrolledPeer,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    HouseholdCreateRequest,
    HouseholdMachine,
    HouseholdResponse,
    PairingTokenIssueResponse,
    SignalEndpointUpdate,
    SignalError,
    SignalHello,
    SignalPing,
    SignalPong,
    SignalPunchCoord,
    SignalRequestPunch,
    SignalRosterUpdate,
    SignalWelcome,
    safe_endpoint_or_none,
)
from coordinator.middleware.auth import coordinator_token_matches
from coordinator.middleware.node_auth import (
    enforce_node_scope,
    extract_bearer_token,
    hash_node_token,
    resolve_node_from_token,
)
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.services import circle_links
from coordinator.services.household_allocator import (
    HouseholdCIDRExhausted,
    allocate_household_cidr,
)
from coordinator.services.mesh_allocator import MeshIPExhausted, allocate_mesh_ip

# How long to wait for the client's first `hello` message before
# dropping the connection. 10s is generous on any real network.
SIGNAL_HELLO_TIMEOUT_S = 10.0

# In-memory map: mesh_node.id → open WebSocket. Populated when a peer
# hellos in, removed on disconnect. Used to broadcast roster updates
# + coordinate hole-punches. Single-process: a multi-process / multi-
# region coordinator deployment needs to lift this into Redis or a
# coordinator-internal pub/sub. Out of scope for v0.
_signal_connections: dict[uuid.UUID, WebSocket] = {}

# In-memory map: mesh_node.id → last-reported private-LAN candidates
# ("ip:port"). Populated from endpoint_update messages and read back in
# _peer_to_enrolled so the list flows into Welcome / RosterUpdate /
# EnrollResponse without a DB column. Deliberately not persisted: LAN
# endpoints are volatile (DHCP, interface churn) and are only useful while
# a node is connected. Single-process, like _signal_connections; a multi-
# process deployment would lift this into shared state. Cleared on disconnect.
_local_endpoints: dict[uuid.UUID, list[str]] = {}

# In-memory map: mesh_node.id → the features that node's sidecar advertised in
# its hello (e.g. "circle_link_v1"). Connection-scoped like _local_endpoints:
# set when the node connects, cleared when its live connection goes. A node
# that sent no features, or is not connected, has none, which is how an old
# sidecar is kept out of direct circle links.
#
# The set is what the coordinator HONOURS, not what the hello said: the link
# feature (circle_link_v1) is kept only when the connection was opened with that
# node's own node_token. A WireGuard public key is not a secret, so a connection
# that names a node by its key alone (the legacy path) must never be offered
# link messages meant for that node.
_signal_features: dict[uuid.UUID, frozenset[str]] = {}


@dataclass(frozen=True)
class _EndpointReport:
    """What one signal connection said its node's endpoints are."""

    ws: WebSocket
    external_endpoint: str
    local_endpoints: tuple[str, ...]


# In-memory map: mesh_node.id → the endpoints the node's own live signal
# connection last reported, kept only for a connection opened with the node's own
# node_token. This is the one endpoint source the direct circle link broker reads
# (signal_link_endpoints). The broker never reads MeshNode.last_endpoint or
# `_local_endpoints`: those feed the household roster and punch coordination, and
# a value in them can be older than the live connection (a row stored before
# endpoints were tied to the node token, say), so it is not proof that this
# machine said it.
#
# A report is made in one place, the endpoint_update handler, and only while the
# reporting connection is a token-bound one and the node's live connection. It is
# dropped when another connection takes the node's slot and when the live one
# goes, so a connection starts with no report and has to send its own
# endpoint_update (a sidecar does so right after every welcome).
_endpoint_reports: dict[uuid.UUID, _EndpointReport] = {}

# Lock guarding `_signal_connections` so concurrent welcome / disconnect
# / broadcast operations don't race on dict mutation. Also guards
# `_local_endpoints`, `_signal_features` and `_endpoint_reports`.
_signal_lock = asyncio.Lock()

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["mesh"])

# How long a pairing token stays valid. 1 hour per design doc:
# long enough for cross-room walk-and-install, short enough that a
# leaked token is low-impact.
PAIRING_TOKEN_TTL = timedelta(hours=1)

# How long a DERP auth JWT is valid. The DERP client refreshes every
# 50 minutes, so a 1-hour TTL gives a 10-minute buffer before expiry.
DERP_TOKEN_TTL = timedelta(hours=1)

# Max allowed clock skew (seconds) between a DERP-token PoP `nonce`
# (a caller-generated unix timestamp) and the coordinator's own clock.
# Bounds signature replay: a captured (nonce, signature) pair is only
# usable within this window. Generous enough for real clock drift +
# network latency, tight enough that a replayed pair goes stale fast.
DERP_NONCE_MAX_SKEW_S = 120


def _generate_pairing_token() -> str:
    """32-char base32 string ≈ 160 bits of entropy. Matches the column
    type in `models.pairing.PairingToken` (String(32))."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


# Raw byte length of a freshly-minted node_token. >=32 bytes (256 bits) per
# the node-token-hardening contract - this token now authorizes ongoing
# machine calls on its own, so it needs the same order-of-magnitude entropy
# as the pairing token that mints it.
NODE_TOKEN_BYTES = 32


def _generate_node_token() -> str:
    """Fresh per-node bearer credential, minted once at successful enroll.
    urlsafe-base64 (no padding) of 32 CSPRNG bytes - 256 bits of entropy.
    The coordinator stores only `hash_node_token(...)`; the raw value is
    returned in `EnrollResponse.node_token` and never persisted or logged."""
    return secrets.token_urlsafe(NODE_TOKEN_BYTES)


# ---------------------------------------------------------------------------
# POST /mesh/households
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/households",
    response_model=HouseholdResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_household(
    req: HouseholdCreateRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> HouseholdResponse:
    """Create the calling member's household. Idempotent: if a household
    already exists for this member, return it instead of creating
    a duplicate (avoids the UNIQUE-mesh_cidr collision on second call).

    This is the standard "first-run create-household" call any member makes
    once. The member is the dev fixture STUB_MEMBER_ID until
    POTLUCK_SESSION_AUTH_ENABLED is set, then the logged-in member.

    Auth: this is a user action (a logged-in member managing their own
    household), so it's gated only by `resolve_session_member_id` (the
    member's session), not by any machine credential. Neither the
    coordinator token nor a per-node `node_token` is ever needed in the
    browser/desktop-app UI that calls this route. Contrast with the
    machine-plumbing routes below (/mesh/enroll, /mesh/heartbeat,
    /mesh/signal, /mesh/peers, /mesh/derp-token, /mesh/nodes/{id}), which
    are gated by `resolve_node_from_token` (per-node `node_token` OR the
    legacy coordinator token - see `middleware/node_auth.py`)."""
    # If this member already has a household, return it.
    existing = await db.execute(
        select(Household).where(Household.owner_member_id == member_id)
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return HouseholdResponse(
            id=found.id,
            owner_member_id=found.owner_member_id,
            name=found.name,
            mesh_cidr=str(found.mesh_cidr),
            created_at=found.created_at,
        )

    # Allocate a fresh /24 from the global pool.
    try:
        mesh_cidr = await allocate_household_cidr(db)
    except HouseholdCIDRExhausted:
        raise HTTPException(
            status_code=503,
            detail="global household pool exhausted",
        )

    household = Household(
        owner_member_id=member_id,
        name=req.name,
        mesh_cidr=mesh_cidr,
    )
    db.add(household)
    await db.commit()
    await db.refresh(household)

    logger.info(
        "household_created",
        household_id=str(household.id),
        mesh_cidr=mesh_cidr,
    )

    return HouseholdResponse(
        id=household.id,
        owner_member_id=household.owner_member_id,
        name=household.name,
        mesh_cidr=str(household.mesh_cidr),
        created_at=household.created_at,
    )


# ---------------------------------------------------------------------------
# GET /mesh/households/current
# ---------------------------------------------------------------------------


@router.get(
    "/mesh/households/current",
    response_model=CurrentHouseholdResponse,
)
async def get_current_household(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CurrentHouseholdResponse:
    """Return the calling member's household and the machines already enrolled
    in it, so the desktop app can offer a one-tap "join your household"
    (no pairing code) when a signed-in machine isn't paired yet - and never
    create a duplicate household by mistake.

    Auth: session-only (`resolve_session_member_id`), same as
    POST /mesh/households - this is a member reading their OWN household and
    OWN machines, so no node/coordinator token is involved. Returns
    household=null when the member has no household yet (the caller is the
    first machine)."""
    existing = await db.execute(
        select(Household).where(Household.owner_member_id == member_id)
    )
    found = existing.scalar_one_or_none()
    if found is None:
        return CurrentHouseholdResponse(household=None, machines=[])

    nodes_result = await db.execute(
        select(MeshNode).where(MeshNode.household_id == found.id)
    )
    machines = [
        HouseholdMachine(
            node_id=n.id,
            node_name=n.node_name,
            platform=n.platform,
            mesh_ip=str(n.mesh_ip),
            last_seen_at=n.last_seen_at,
        )
        for n in nodes_result.scalars().all()
    ]
    return CurrentHouseholdResponse(
        household=HouseholdResponse(
            id=found.id,
            owner_member_id=found.owner_member_id,
            name=found.name,
            mesh_cidr=str(found.mesh_cidr),
            created_at=found.created_at,
        ),
        machines=machines,
    )


# ---------------------------------------------------------------------------
# DELETE /mesh/households/current/machines/{node_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/mesh/households/current/machines/{node_id}",
    status_code=204,
    response_class=Response,
)
async def remove_household_machine(
    node_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Remove a machine from the calling member's OWN household - the
    member-facing counterpart to the node-authed DELETE /mesh/nodes/{id}
    (peer cleanup). Lets someone drop a stale/ghost node (a machine that was
    wiped or reinstalled without unpairing) from their roster, so the app
    doesn't keep showing dead machines or trying to reach them.

    Auth: session-only (`resolve_session_member_id`) - a member managing their
    own household, no node/coordinator token. The target must belong to the
    member's household (else 403), and the member must have a household (else
    404). Idempotent: 204 even if the node is already gone.

    On success: deletes the row + broadcasts SignalRosterUpdate(removed) so
    online household-mates drop it from their live roster immediately."""
    household_result = await db.execute(
        select(Household).where(Household.owner_member_id == member_id)
    )
    household = household_result.scalar_one_or_none()
    if household is None:
        raise HTTPException(status_code=404, detail="no household for this member")

    target = await db.get(MeshNode, node_id)
    if target is None:
        return Response(status_code=204)  # idempotent
    if target.household_id != household.id:
        # Not the member's machine - don't confirm existence across households.
        raise HTTPException(status_code=403, detail="not a machine in your household")

    target_id_str = str(target.id)
    household_id = target.household_id

    # Direct circle links to or from this machine end first: the rows would
    # otherwise vanish with the node (ON DELETE CASCADE) while the other
    # household's sidecar still held the peer.
    ended_links = await circle_links.guarded_end(db, circle_links.end_for_node, target.id)

    await db.delete(target)
    await db.commit()
    await circle_links.push_removed(ended_links)

    logger.info(
        "mesh_node_removed_by_member",
        node_id=target_id_str,
        household_id=str(household_id),
        member_id=str(member_id),
    )

    async with get_session_factory()() as broadcast_db:
        await _broadcast_to_household(
            household_id,
            SignalRosterUpdate(removed=[target_id_str]).model_dump(mode="json"),
            db=broadcast_db,
            except_node_id=None,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /mesh/pairing-tokens
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/pairing-tokens",
    response_model=PairingTokenIssueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_pairing_token(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> PairingTokenIssueResponse:
    """Issue a fresh single-use pairing token for the calling member's
    household. Returns 404 if the member has no household yet (must
    POST /mesh/households first).

    The plaintext token value is returned only in this response. The
    coordinator stores it as the primary key in `pairing_tokens`; if
    the user loses the value they need to issue a new one (rather than
    look it up server-side).

    Auth: same posture as POST /mesh/households - this is a user action
    (the logged-in member issuing a token for their own household), gated
    only by the member's session, not the coordinator token. The token
    issued here (the pairing token) is what subsequently authorizes the
    machine-level POST /mesh/enroll call - that's the credential handoff
    from user-space to machine-space, and it's why /mesh/enroll itself
    deliberately needs no session."""
    household_result = await db.execute(
        select(Household).where(Household.owner_member_id == member_id)
    )
    household = household_result.scalar_one_or_none()
    if household is None:
        raise HTTPException(
            status_code=404,
            detail="no household for caller - POST /mesh/households first",
        )

    now = datetime.now(timezone.utc)
    token_value = _generate_pairing_token()
    token = PairingToken(
        token=token_value,
        household_id=household.id,
        issued_by_member_id=member_id,
        expires_at=now + PAIRING_TOKEN_TTL,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)

    logger.info(
        "pairing_token_issued",
        household_id=str(household.id),
        token_prefix=token_value[:8],
        expires_at=token.expires_at.isoformat(),
    )

    return PairingTokenIssueResponse(
        token=token_value,
        household_id=household.id,
        expires_at=token.expires_at,
        created_at=token.created_at,
    )


# ---------------------------------------------------------------------------
# POST /mesh/enroll
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/enroll",
    response_model=EnrollResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enroll(
    req: EnrollRequest,
    db: AsyncSession = Depends(get_db),
    x_potluck_coordinator_token: str | None = Header(
        default=None, alias="X-Potluck-Coordinator-Token"
    ),
) -> EnrollResponse:
    """Redeem a pairing token, allocate a mesh IP, persist the node,
    return the household's roster + a fresh per-node bearer credential.

    Flow:
      1. Look up the pairing token. Must exist, must be unconsumed,
         must not be expired.
      2. Load the household.
      3. Allocate a mesh IP from the household's CIDR.
      4. Create the mesh_nodes row (with a freshly-minted node_token_hash).
      5. Mark the pairing_token consumed.
      6. Return the new node's mesh IP + the household roster + the raw
         node_token (shown exactly once).

    Auth: a valid pairing token
    (unconsumed, unexpired, resolves to a real household) is sufficient
    auth on its own - an app-spawned sidecar enrolls with nothing but the
    pairing token a member handed it, never the machine-wide
    POTLUCK_COORDINATOR_TOKEN. When the pairing token alone doesn't verify,
    the coordinator token is still accepted (back-compat for clients that
    enroll with `--coordinator-token` and a shared pairing
    token). Neither present/valid → 401. This does not relax pairing-token
    validation itself: a bogus/expired/reused token still fails with its
    specific 404/409/410 below even when a valid coordinator token is
    presented - the coordinator token only ever substitutes for "no
    pairing token needed", never for "any pairing token accepted".

    On any failure that's the caller's fault → 4xx with a categorical
    reason. Internal errors → 5xx with a generic message; structured
    logs carry the detail.
    """
    now = datetime.now(timezone.utc)

    # ---- 0. Auth: valid pairing token is sufficient on its own; else fall
    #      back to the coordinator token (back-compat). ---- #
    # with_for_update: SELECT ... FOR UPDATE serializes concurrent redemptions
    # of the same single-use token. Without the row lock, two enrolls racing
    # under READ COMMITTED both read consumed_at=None and both succeed - one
    # token enrolls N rogue nodes into the household. The lock makes the second
    # redemption block, re-read consumed_at as set, and correctly 409.
    token_row = await db.get(
        PairingToken, req.pairing_token, with_for_update=True
    )
    pairing_token_valid = (
        token_row is not None
        and token_row.consumed_at is None
        and token_row.expires_at > now
    )
    if not pairing_token_valid and not coordinator_token_matches(
        x_potluck_coordinator_token
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    # ---- 1. Verify the pairing token (surface the specific reason) ---- #
    if token_row is None:
        raise HTTPException(status_code=404, detail="pairing token not found")
    if token_row.consumed_at is not None:
        raise HTTPException(status_code=409, detail="pairing token already consumed")
    if token_row.expires_at <= now:
        raise HTTPException(status_code=410, detail="pairing token expired")

    # ---- 2. Load the household ---- #
    household = await db.get(Household, token_row.household_id)
    if household is None:
        # Token references a household that no longer exists. This is a
        # data-integrity bug, not a client error - but the client can't
        # do anything about it either way. Surface as 409.
        logger.error(
            "pairing_token_orphaned_household",
            token_prefix=req.pairing_token[:8],
            household_id=str(token_row.household_id),
        )
        raise HTTPException(status_code=409, detail="household no longer exists")

    # ---- 3. Allocate a mesh IP ---- #
    try:
        mesh_ip = await allocate_mesh_ip(db, household.id, household.mesh_cidr)
    except MeshIPExhausted:
        raise HTTPException(
            status_code=503,
            detail="household IP range exhausted; contact support",
        )

    # ---- 4. Reject duplicate WG pubkeys early ---- #
    # The UNIQUE constraint will catch this, but a structured 409 is a
    # better client experience than an opaque 500-on-IntegrityError.
    existing_by_key = await db.execute(
        select(MeshNode).where(MeshNode.wg_public_key == req.wg_public_key)
    )
    if existing_by_key.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="wg_public_key already enrolled (possibly to a different household)",
        )

    # ---- 4b. Reject duplicate node_name in this household early ---- #
    # There's a UNIQUE (household_id, node_name) constraint at the DB
    # level; surfacing it as a structured 409 here is friendlier than a
    # generic 500-on-IntegrityError.
    name_conflict = await db.execute(
        select(MeshNode).where(
            MeshNode.household_id == household.id,
            MeshNode.node_name == req.node_name,
        )
    )
    if name_conflict.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"node_name '{req.node_name}' already exists in this household; "
                "pick a different name"
            ),
        )

    # ---- 4c. Validate the optional PoP signing key up front ---- #
    # Optional - absent for any mesh client without DERP-token PoP
    # support (back-compat). If present, validate it's actually a decodable
    # Ed25519 key now rather than storing garbage that would just fail every
    # future derp-token verification silently.
    if req.signing_public_key is not None:
        try:
            decode_ed25519_public_key(req.signing_public_key, field_name="signing_public_key")
        except KeyDecodeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- 5. Persist the new node ---- #
    # Mint the per-node bearer credential now, before insert, so only its
    # hash ever touches the DB (or logs) - the raw value lives in this local
    # variable until it's placed on the response below.
    node_token = _generate_node_token()
    node = MeshNode(
        household_id=household.id,
        node_name=req.node_name,
        mesh_ip=mesh_ip,
        wg_public_key=req.wg_public_key,
        signing_public_key=req.signing_public_key,
        platform=req.platform,
        node_token_hash=hash_node_token(node_token),
    )
    db.add(node)
    await db.flush()  # populate node.id

    token_row.consumed_at = now
    token_row.consumed_by_node_id = node.id

    await db.commit()
    await db.refresh(node)

    # ---- 6. Build the roster ---- #
    roster_result = await db.execute(
        select(MeshNode).where(
            MeshNode.household_id == household.id,
            MeshNode.id != node.id,
        )
    )
    peers = [_peer_to_enrolled(p) for p in roster_result.scalars().all()]

    logger.info(
        "mesh_node_enrolled",
        node_id=str(node.id),
        household_id=str(household.id),
        mesh_ip=mesh_ip,
        platform=req.platform or "unknown",
        roster_size=len(peers),
    )

    return EnrollResponse(
        node_id=node.id,
        household_id=household.id,
        mesh_ip=mesh_ip,
        mesh_cidr=str(household.mesh_cidr),
        peers=peers,
        node_token=node_token,
    )


# ---------------------------------------------------------------------------
# GET /mesh/peers
# ---------------------------------------------------------------------------


@router.get(
    "/mesh/peers",
    response_model=list[EnrolledPeer],
)
async def list_peers(
    wg_public_key: str,
    auth_node: MeshNode | None = Depends(resolve_node_from_token),
    db: AsyncSession = Depends(get_db),
) -> list[EnrolledPeer]:
    """Return the household roster for a node, identified by its
    `wg_public_key` query parameter.

    Gated by `resolve_node_from_token` (mirrors every sibling /mesh/* route)
    so an internet-facing coordinator can't be enumerated by anyone who
    merely knows/guesses a registered wg_public_key. Accepts either a
    per-node `node_token` (bound to exactly that node - see below) or the
    legacy coordinator token (back-compat, no per-node binding).

    v0 auth model: possession of the WG pubkey is treated as proof
    of household membership. This is acceptable for v0 because:
      * the pubkey is registered during enrollment via a one-time
        pairing token (so an attacker can't enroll arbitrarily),
      * the roster contains only mesh-layer info, no inference content,
      * the WG pubkey itself is not secret (it's exchanged with peers
        anyway).
    Stronger auth (signed-nonce challenge) is a v1 hardening item.
    """
    result = await db.execute(
        select(MeshNode).where(MeshNode.wg_public_key == wg_public_key)
    )
    caller = result.scalar_one_or_none()
    if caller is None:
        raise HTTPException(status_code=404, detail="node not found")
    enforce_node_scope(auth_node, caller.id)

    roster_result = await db.execute(
        select(MeshNode).where(
            MeshNode.household_id == caller.household_id,
            MeshNode.id != caller.id,
        )
    )
    return [_peer_to_enrolled(p) for p in roster_result.scalars().all()]


# ---------------------------------------------------------------------------
# DELETE /mesh/nodes/{node_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/mesh/nodes/{node_id}",
    status_code=204,
    response_class=Response,
)
async def delete_mesh_node(
    node_id: uuid.UUID,
    wg_public_key: str,
    auth_node: MeshNode | None = Depends(resolve_node_from_token),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a node from a household. Idempotent - returns 204 even if the
    node row is already gone (re-running the cleanup is safe).

    Gated by `resolve_node_from_token`: either a per-node `node_token`
    (bound to the caller - the `wg_public_key` query param must be that
    node's own key, enforced below; the target may still be a different
    household-mate, e.g. peer cleanup of a stale entry) or the legacy
    coordinator token (back-compat, no per-node binding).

    v0 auth model: the caller proves household membership with their own
    wg_public_key. Either:
      * the caller IS the node being deleted (self-leave), OR
      * the caller is a household-mate of the target (peer cleanup of a
        stale roster entry - e.g. a paired machine that died or was
        re-paired with a fresh key).
    A signed-nonce challenge is v1 hardening; for v0, possession of any
    enrolled household key is sufficient.

    On success: deletes the row + broadcasts SignalRosterUpdate(removed)
    to the surviving household-mates currently on the signal WS.
    """
    # Look up the caller by their WG key to bound this delete to their
    # household. Random pubkeys can't delete anything.
    caller_result = await db.execute(
        select(MeshNode).where(MeshNode.wg_public_key == wg_public_key)
    )
    caller = caller_result.scalar_one_or_none()
    if caller is None:
        raise HTTPException(status_code=403, detail="wg_public_key not enrolled")
    enforce_node_scope(auth_node, caller.id)

    target = await db.get(MeshNode, node_id)
    if target is None:
        # Already gone - return 204 anyway. Idempotent cleanup is more
        # ergonomic than forcing the client to special-case 404.
        return Response(status_code=204)

    if target.household_id != caller.household_id:
        # Don't leak whether the node exists across households - return 403
        # like any other unauthorized delete.
        raise HTTPException(
            status_code=403, detail="caller is not in target's household"
        )

    target_id_str = str(target.id)
    household_id = target.household_id

    ended_links = await circle_links.guarded_end(db, circle_links.end_for_node, target.id)

    await db.delete(target)
    await db.commit()
    await circle_links.push_removed(ended_links)

    logger.info(
        "mesh_node_deleted",
        node_id=target_id_str,
        household_id=str(household_id),
        deleted_by_prefix=wg_public_key[:8],
    )

    # Broadcast removal to surviving household-mates on the signal WS.
    # The except_node_id filter doesn't matter here (the target is already
    # gone) but the household scope does.
    async with get_session_factory()() as broadcast_db:
        await _broadcast_to_household(
            household_id,
            SignalRosterUpdate(removed=[target_id_str]).model_dump(mode="json"),
            db=broadcast_db,
            except_node_id=None,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /mesh/heartbeat
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/heartbeat",
    response_model=HeartbeatResponse,
)
async def heartbeat(
    req: HeartbeatRequest,
    auth_node: MeshNode | None = Depends(resolve_node_from_token),
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    """Update a node's `last_endpoint` + `last_seen_at`.

    Same v0 auth model as /mesh/peers: the (node_id, wg_public_key)
    pair must match an existing row. v1 will move to a signed-nonce
    flow. Gated by `resolve_node_from_token`: either a per-node
    `node_token` (bound to exactly `req.node_id`, enforced below) or the
    legacy coordinator token (back-compat, no per-node binding).

    The endpoint is stricter than the rest. If the node has a node token, only
    a request that presented it may set `last_endpoint`; with the legacy
    credential the heartbeat still succeeds and updates `last_seen_at`, and the
    endpoint in the body is dropped. The WireGuard key that identifies the node
    here is public, and a node's stored endpoint can reach another household's
    sidecar in a direct circle link message.
    """
    node = await db.get(MeshNode, req.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    if node.wg_public_key != req.wg_public_key:
        # Identifying as a different node than the caller's key matches
        # → reject with 403, not 404, to make the failure mode clear.
        raise HTTPException(status_code=403, detail="wg_public_key mismatch")
    enforce_node_scope(auth_node, node.id)

    # A machine with a node token has its endpoint written only by a request
    # that presented that token. `auth_node` is set only by a valid node_token
    # (and enforce_node_scope has just shown it is this node's), so with the
    # legacy credential, which names the node by its non-secret WireGuard key,
    # the request still counts as a heartbeat but the endpoint is left alone. A
    # node enrolled before tokens existed has no hash and is handled as before.
    if node.node_token_hash is None or auth_node is not None:
        node.last_endpoint = req.external_endpoint
    else:
        logger.info("mesh_heartbeat_endpoint_needs_node_token", node_id=str(node.id))
    node.last_seen_at = datetime.now(timezone.utc)
    await db.commit()

    return HeartbeatResponse(server_time=node.last_seen_at)


# ---------------------------------------------------------------------------
# POST /mesh/derp-token
# ---------------------------------------------------------------------------


def _verify_derp_pop(node: MeshNode, req: DERPTokenRequest) -> None:
    """Verify the optional proof-of-possession signature on a DERP-token
    request. Raises HTTPException on any failure. No-op (returns silently)
    when the request carries no signature and PoP isn't required - the
    caller is responsible for logging that fallback.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    try:
        nonce_ts = int(req.nonce)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="nonce must be a unix timestamp")
    if abs(now_ts - nonce_ts) > DERP_NONCE_MAX_SKEW_S:
        raise HTTPException(status_code=401, detail="nonce is stale or from the future")

    if node.signing_public_key is None:
        # Caller tried to prove possession but this node never registered a
        # signing key (enrolled before PoP hardening, or omitted it). This is
        # a client/config error, not something to silently paper over by
        # falling back to the legacy check - the caller explicitly asked for
        # verification we can't perform.
        raise HTTPException(
            status_code=401,
            detail="signature provided but no signing_public_key registered for this node",
        )

    message = f"derp-token:{req.wg_public_key}:{req.region}:{req.nonce}".encode()
    if not verify_ed25519_signature(node.signing_public_key, req.signature, message):  # type: ignore[arg-type]
        raise HTTPException(status_code=401, detail="signature does not verify")


@router.post(
    "/mesh/derp-token",
    response_model=DERPTokenResponse,
)
async def issue_derp_token(
    req: DERPTokenRequest,
    auth_node: MeshNode | None = Depends(resolve_node_from_token),
    db: AsyncSession = Depends(get_db),
) -> DERPTokenResponse:
    """Issue a short-lived JWT that authorises a mesh node to connect to
    the DERP relay.

    Gated by `resolve_node_from_token`: either a per-node `node_token`
    (bound to exactly `req.wg_public_key`'s node, enforced below) or the
    legacy coordinator token (back-compat, no per-node binding).

    Auth model: possession of a registered wg_public_key is the base
    credential - the coordinator verifies the key exists in mesh_nodes. This
    alone is weak: wg_public_key is not secret (nodes exchange it with peers
    for the WG handshake), so anyone who has observed it can mint a token as
    that node.

    Proof-of-possession hardening (back-compat, additive - see
    DERPTokenRequest's docstring): optional `nonce` + `signature` prove the
    caller holds the
    private key for a separately-registered `MeshNode.signing_public_key`
    (Ed25519 - distinct from the WG/Curve25519 key, which isn't usable for
    signature verification). When both are present they are always verified
    (mismatch -> 401, regardless of `derp_pop_required`). When absent:
      * `derp_pop_required=False` (default) - legacy behaviour: the
        wg_public_key-possession check alone is accepted, and we log a
        warning so operators can see how much of their fleet hasn't
        upgraded. Needed because the current Go mesh client
        (coordapi/client.go) does not yet sign this challenge - flipping
        the flag on before that ships would lock out every enrolled node.
      * `derp_pop_required=True` - the request is rejected (401). Only
        safe once the Go client signs by default.

    Privacy contract:
      * JWT claims: wg_pubkey + region + exp + iat only.
      * No member_id, no household_id, no node_id leaks into the JWT.
      * The relay therefore never learns the caller's identity.
      * We log only the truncated pubkey (first 8 chars), never the token
        or the signature.
    """
    # ---- look up the node ---- #
    result = await db.execute(
        select(MeshNode).where(MeshNode.wg_public_key == req.wg_public_key)
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise HTTPException(
            status_code=404,
            detail="wg_public_key not enrolled",
        )
    enforce_node_scope(auth_node, node.id)

    # ---- proof-of-possession (optional, back-compat) ---- #
    settings = get_settings()
    if req.signature is not None or req.nonce is not None:
        if req.signature is None or req.nonce is None:
            raise HTTPException(
                status_code=400,
                detail="nonce and signature must both be present or both absent",
            )
        _verify_derp_pop(node, req)
        logger.info("derp_token_pop_verified", pubkey_prefix=req.wg_public_key[:8])
    elif settings.derp_pop_required:
        raise HTTPException(
            status_code=401,
            detail="proof-of-possession (nonce + signature) is required",
        )
    else:
        logger.warning(
            "derp_token_issued_without_pop",
            pubkey_prefix=req.wg_public_key[:8],
            reason="caller omitted nonce/signature - accepted under legacy "
            "wg_public_key-possession-only auth (v0.5 PoP hardening, "
            "not yet mandatory; see DERPTokenRequest docstring)",
        )

    # ---- load the signing key ---- #
    jwt_key = os.environ.get("POTLUCK_DERP_JWT_KEY", "")
    if not jwt_key:
        raise HTTPException(
            status_code=503,
            detail="DERP relay not configured (POTLUCK_DERP_JWT_KEY missing)",
        )

    # ---- build the JWT ---- #
    # The wg_pubkey claim is base64-encoded so the relay can decode the raw
    # 32-byte key. The coordinator stores it as a base64 string already.
    now = datetime.now(timezone.utc)
    expires_at = now + DERP_TOKEN_TTL

    claims = {
        "wg_pubkey": req.wg_public_key,  # base64-encoded 32-byte WG pubkey
        "region": req.region,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }

    token = jwt.encode(claims, jwt_key, algorithm="HS256")

    # Log the truncated pubkey only - never the token or full claims.
    logger.info(
        "derp_token_issued",
        pubkey_prefix=req.wg_public_key[:8],
        region=req.region,
        expires_at=expires_at.isoformat(),
    )

    return DERPTokenResponse(
        token=token,
        expires_at=expires_at.isoformat(),
        region=req.region,
    )


# ---------------------------------------------------------------------------
# WS /mesh/signal
# ---------------------------------------------------------------------------


def _peer_to_enrolled(peer: MeshNode) -> EnrolledPeer:
    # local_endpoints are not persisted; pull the last value reported over the
    # signal WS from the in-memory map (empty if the node never reported any or
    # isn't currently connected). Read without the lock: dict.get is atomic in
    # CPython and a stale read is harmless (worst case a peer learns a slightly
    # old LAN candidate, which this phase doesn't act on anyway).
    return EnrolledPeer(
        node_id=peer.id,
        node_name=peer.node_name,
        mesh_ip=str(peer.mesh_ip),
        wg_public_key=peer.wg_public_key,
        last_endpoint=peer.last_endpoint,
        local_endpoints=list(_local_endpoints.get(peer.id, [])),
        last_seen_at=peer.last_seen_at,
        platform=peer.platform,
    )


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    """Send a JSON payload to a peer's WS. Returns True on success,
    False if the peer is gone. Never raises."""
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def signal_send(node_id: uuid.UUID, payload: dict) -> bool:
    """Send one payload to a single connected node. False if the node is not
    connected or the send failed (a dead connection is dropped from the
    registry, as _broadcast_to_household does). Never raises.

    Callers build `payload` from a Signal* model; nothing is added here. The
    direct circle link service does not call this: it goes through
    signal_send_link, which adds the node-token gate.
    """
    return await _send_to_node(node_id, payload, require_feature=None)


async def signal_send_link(node_id: uuid.UUID, payload: dict) -> bool:
    """Send one circle_peer_* message (add, extend or remove) to a node, only if
    its live connection is one the coordinator offers links on: it advertised
    circle_link_v1 AND was opened with the node's own node_token (a legacy
    connection never keeps the feature, see mesh_signal).

    The check and the lookup of the socket happen under one lock hold, so a
    connection that takes the node's slot between a caller's own check and this
    send (a legacy hello for the same key, say) still cannot be written to. Every
    link message goes through here, removes included: a remove carries only a
    link id and a reason, but sending it would still tell whoever holds the
    channel that this machine has a link and when it ended.

    False means "not delivered" and is treated like an offline sidecar. Never
    raises."""
    return await _send_to_node(node_id, payload, require_feature=circle_links.LINK_FEATURE)


async def _send_to_node(
    node_id: uuid.UUID, payload: dict, *, require_feature: str | None
) -> bool:
    async with _signal_lock:
        ws = _signal_connections.get(node_id)
        if (
            ws is not None
            and require_feature is not None
            and require_feature not in _signal_features.get(node_id, frozenset())
        ):
            ws = None
    if ws is None:
        return False
    ok = await _safe_send(ws, payload)
    if not ok:
        async with _signal_lock:
            if _signal_connections.get(node_id) is ws:
                _signal_connections.pop(node_id, None)
    return ok


def signal_node_connected(node_id: uuid.UUID) -> bool:
    """True if the node has a live signal connection. A plain read of the
    registry, like _peer_to_enrolled's: a stale answer costs one failed send."""
    return node_id in _signal_connections


def signal_node_features(node_id: uuid.UUID) -> frozenset[str]:
    """The features the coordinator honours on the node's live connection: what
    the sidecar advertised, minus circle_link_v1 unless the connection was
    opened with the node's own node_token."""
    return _signal_features.get(node_id, frozenset())


def signal_link_endpoints(node_id: uuid.UUID) -> tuple[str | None, list[str]]:
    """The node's public endpoint and private LAN candidates, for a direct circle
    link message to another household. (None, []) unless the node's live
    connection is a token-bound one that has itself reported them.

    Reads _endpoint_reports and nothing else, so a value stored through any other
    path (a legacy signal connection, a heartbeat, a row from before endpoints
    were tied to the node token) is never returned. A report counts only while
    the connection that made it is still the node's live connection. The LAN
    candidates were filtered to RFC1918 when they were stored."""
    report = _endpoint_reports.get(node_id)
    if report is None or _signal_connections.get(node_id) is not report.ws:
        return None, []
    return report.external_endpoint, list(report.local_endpoints)


async def _broadcast_to_household(
    household_id: uuid.UUID,
    payload: dict,
    *,
    db: AsyncSession,
    except_node_id: uuid.UUID | None = None,
) -> None:
    """Send a payload to every connected peer in a household.

    Skips `except_node_id` (typically the originating peer for an
    endpoint-update broadcast). Drops disconnected peers from the
    registry."""
    # Load the set of household members so we know which connections
    # to talk to. Note this queries by household_id only - connections
    # for nodes that aren't in this household are ignored.
    result = await db.execute(
        select(MeshNode.id).where(MeshNode.household_id == household_id)
    )
    household_node_ids = {row[0] for row in result.all()}

    async with _signal_lock:
        targets = [
            (nid, ws)
            for nid, ws in _signal_connections.items()
            if nid in household_node_ids and nid != except_node_id
        ]
    # Send outside the lock so a slow peer doesn't block others.
    dead: list[tuple[uuid.UUID, object]] = []
    for nid, ws in targets:
        ok = await _safe_send(ws, payload)
        if not ok:
            dead.append((nid, ws))
    if dead:
        async with _signal_lock:
            for nid, ws in dead:
                # Only evict if the registry still holds this ws - a peer
                # that reconnected between the snapshot and here has already
                # replaced the slot with a new live connection.
                if _signal_connections.get(nid) is ws:
                    _signal_connections.pop(nid, None)


@router.websocket("/mesh/signal")
async def mesh_signal(ws: WebSocket) -> None:
    """Long-lived signaling channel for one mesh node.

    Lifecycle:
      1. Client connects, server accepts.
      2. Client sends SignalHello { wg_public_key }.
      3. Server validates against mesh_nodes, replies with SignalWelcome
         + current roster. Server broadcasts SignalRosterUpdate to other
         household members ('added' on first connect, 'updated' otherwise).
      4. Server + client exchange messages until disconnect.
      5. On disconnect: server removes the connection from the registry
         and broadcasts the node's `removed` to the household. A connection
         that a newer one for the same node displaced does not broadcast it:
         the node is still there.

    Auth: the WS upgrade is gated by either a per-node `node_token`
    (`Authorization: Bearer <node_token>` header on the upgrade request -
    same header/format as the REST machine routes) or the legacy
    coordinator token (`X-Potluck-Coordinator-Token`, when configured;
    back-compat). A resolved node_token additionally binds this connection
    to exactly that node: once `hello.wg_public_key` is known (step 2
    below), it must match the token's own node, or the connection is
    rejected - a node_token must only ever open the signal channel as
    itself. Possession of a valid `wg_public_key` alone (no credential
    matched, dev-mode passthrough) still identifies the node.
    Stronger auth (signed-nonce member<->key challenge mirroring
    /peers/connect) is a v1 hardening item.

    Direct circle links are the exception to that. A WireGuard public
    key is not a secret, so a connection that names a node by its key alone
    must not receive that node's link messages (they carry the other
    household's key, address and, for a lender, the token hash). Such a
    connection joins, gets its welcome and roster and takes part in the
    household mesh exactly as it always did, but circle_link_v1 is dropped from
    the features the coordinator keeps for it, and no link message is written
    to it (see signal_send_link). Only a connection opened with the node's own
    node_token is offered links. There is no setting that changes this.

    Endpoints follow the same rule. A node that has a node token (a
    node_token_hash) has its endpoints written only by a connection opened with
    that token: an `endpoint_update` on any other connection is neither stored
    nor passed on to the household, and the connection gets one info log line.
    A node enrolled before tokens existed has no hash and its connection writes
    endpoints as it always did. What a direct circle link sends another
    household comes from a narrower store still, see _endpoint_reports.
    """
    await ws.accept()

    # ---- node_token / coordinator-token gate (mirrors /peers/connect) ---- #
    # Try the per-node bearer credential first: `Authorization: Bearer
    # <node_token>`, same header/format as every REST machine route. If it
    # resolves, `auth_node_id` binds this connection to exactly that node -
    # checked against `hello.wg_public_key` once hello arrives (step 2
    # below).
    #
    # If it doesn't resolve (absent, malformed, unknown), fall back to the
    # legacy `X-Potluck-Coordinator-Token` gate - no-op when
    # POTLUCK_COORDINATOR_TOKEN is unset (tokenless household coordinators
    # unaffected).
    auth_node_id: uuid.UUID | None = None
    bearer = extract_bearer_token(ws.headers.get("authorization"))
    if bearer:
        token_hash = hash_node_token(bearer)
        async with get_session_factory()() as auth_db:
            auth_result = await auth_db.execute(
                select(MeshNode.id).where(MeshNode.node_token_hash == token_hash)
            )
            auth_node_id = auth_result.scalar_one_or_none()

    if auth_node_id is None and not coordinator_token_matches(
        ws.headers.get("x-potluck-coordinator-token")
    ):
        logger.info("mesh_signal_rejected_bad_credentials")
        await _safe_send(
            ws,
            SignalError(
                code="unauthorized",
                message="node_token or coordinator token required",
            ).model_dump(mode="json"),
        )
        await ws.close(code=1008)
        return

    # ---- 1. Wait for hello ---- #
    try:
        raw = await asyncio.wait_for(
            ws.receive_json(), timeout=SIGNAL_HELLO_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        await _safe_send(
            ws,
            SignalError(code="hello_timeout", message="no hello in 10s").model_dump(
                mode="json"
            ),
        )
        await ws.close(code=1008)
        return
    except WebSocketDisconnect:
        return

    try:
        hello = SignalHello.model_validate(raw)
    except ValidationError as e:
        await _safe_send(
            ws,
            SignalError(
                code="hello_invalid",
                message=f"invalid hello: {e.error_count()} error(s)",
            ).model_dump(mode="json"),
        )
        await ws.close(code=1008)
        return

    # ---- 2. Look up node by wg_public_key ---- #
    async with get_session_factory()() as db:
        result = await db.execute(
            select(MeshNode).where(MeshNode.wg_public_key == hello.wg_public_key)
        )
        node = result.scalar_one_or_none()
        if node is None:
            await _safe_send(
                ws,
                SignalError(
                    code="node_not_enrolled",
                    message="wg_public_key not found in any household",
                ).model_dump(mode="json"),
            )
            await ws.close(code=1008)
            return

        # A resolved node_token may only open this channel AS its own
        # node - reject if `hello.wg_public_key` names a DIFFERENT node
        # than the one the bearer credential resolved to.
        if auth_node_id is not None and auth_node_id != node.id:
            await _safe_send(
                ws,
                SignalError(
                    code="unauthorized",
                    message="node_token does not authorize this wg_public_key",
                ).model_dump(mode="json"),
            )
            await ws.close(code=1008)
            return

        # Capture immutable fields before leaving the session scope.
        node_id = node.id
        household_id = node.household_id
        mesh_ip = str(node.mesh_ip)

        # Build the initial roster (everyone else in the household).
        roster_result = await db.execute(
            select(MeshNode).where(
                MeshNode.household_id == household_id,
                MeshNode.id != node_id,
            )
        )
        roster = [_peer_to_enrolled(p) for p in roster_result.scalars().all()]

        # Update last_seen_at as a side-effect of hello.
        node.last_seen_at = datetime.now(timezone.utc)
        await db.commit()

        # Snapshot for the roster-broadcast below.
        node_snapshot = _peer_to_enrolled(node)

    # ---- 3. Register + welcome + broadcast added ---- #
    # Did the node's own token open this connection? The check above already
    # guarantees auth_node_id is this node's id whenever it is set; the equality
    # is repeated so this stays true if that check is ever moved.
    token_bound = auth_node_id is not None and auth_node_id == node_id
    # hello.features is already cleaned (SignalHello drops anything malformed,
    # never fails). Direct circle links are offered only on a token-bound
    # connection: a node's WireGuard key is public, so a hello that merely names
    # it must not make the coordinator send link messages here. Everything else
    # about a legacy connection is left as it was.
    features = frozenset(hello.features)
    links_withheld = not token_bound and circle_links.LINK_FEATURE in features
    if links_withheld:
        features = features - {circle_links.LINK_FEATURE}
    async with _signal_lock:
        old_ws = _signal_connections.get(node_id)
        _signal_connections[node_id] = ws
        # Replaces any set from the connection this one displaces, in the same
        # lock hold as the connection itself, so signal_send_link never sees one
        # connection with the other's features.
        _signal_features[node_id] = features
        # A new connection starts with no endpoint report: only what it sends
        # itself, as a token-bound connection, can be given to a link.
        _endpoint_reports.pop(node_id, None)

    if links_withheld:
        logger.info("mesh_signal_links_need_node_token", node_id=str(node_id))

    if old_ws is not None:
        # A previous connection for this node existed - close it cleanly.
        try:
            await old_ws.close(code=1000)
        except Exception:
            pass

    await _safe_send(
        ws,
        SignalWelcome(
            node_id=node_id,
            household_id=household_id,
            mesh_ip=mesh_ip,
            roster=roster,
        ).model_dump(mode="json"),
    )

    # Broadcast "added" to other household peers (this is the moment
    # they can attempt to connect to the new node).
    async with get_session_factory()() as db:
        await _broadcast_to_household(
            household_id,
            SignalRosterUpdate(added=[node_snapshot]).model_dump(mode="json"),
            db=db,
            except_node_id=node_id,
        )

    logger.info(
        "mesh_signal_connected",
        node_id=str(node_id),
        household_id=str(household_id),
        roster_size=len(roster),
        token_bound=token_bound,
    )

    # Direct circle links this node is a party to are sent again, so a sidecar
    # that restarted (and so lost them) picks them up. A no-op unless links are
    # on and this sidecar advertised them. Must never end the signal loop: this
    # channel carries the household's mesh.
    try:
        await circle_links.replay_for_node(node_id)
    except Exception:
        logger.exception("circle_link_replay_failed", node_id=str(node_id))

    # ---- 4. Message loop ---- #
    # One log line per connection when a legacy connection's endpoint_update is
    # ignored (see the endpoint_update branch).
    endpoint_ignored_logged = False
    try:
        while True:
            raw = await ws.receive_json()
            msg_type = raw.get("type") if isinstance(raw, dict) else None

            if msg_type == "ping":
                await _safe_send(
                    ws,
                    SignalPong(server_time=datetime.now(timezone.utc)).model_dump(
                        mode="json"
                    ),
                )
                continue

            if msg_type == "endpoint_update":
                try:
                    upd = SignalEndpointUpdate.model_validate(raw)
                except ValidationError as e:
                    await _safe_send(
                        ws,
                        SignalError(
                            code="endpoint_update_invalid",
                            message=f"{e.error_count()} error(s)",
                        ).model_dump(mode="json"),
                    )
                    continue

                async with get_session_factory()() as db:
                    db_node = await db.get(MeshNode, node_id)
                    if db_node is None:
                        continue
                    if db_node.node_token_hash is not None and not token_bound:
                        # A machine with a node token has its endpoints written
                        # only by something that presented that token. This
                        # connection names the node by its WireGuard key, which
                        # is not a secret, so nothing it sends is stored or
                        # passed on. A node enrolled before tokens existed has
                        # no hash and is handled as it always was.
                        if not endpoint_ignored_logged:
                            endpoint_ignored_logged = True
                            logger.info(
                                "mesh_signal_endpoint_needs_node_token",
                                node_id=str(node_id),
                            )
                        continue

                    # Store the last-reported LAN candidates in-memory (already
                    # filtered to private ip:port + capped by the schema
                    # validator) so they flow into roster snapshots via
                    # _peer_to_enrolled. No DB write for these: they are
                    # volatile and connection-scoped.
                    async with _signal_lock:
                        _local_endpoints[node_id] = list(upd.local_endpoints)
                        if token_bound and _signal_connections.get(node_id) is ws:
                            # The one place an endpoint report is made, see
                            # _endpoint_reports. Only the live connection's
                            # report counts, so a connection that was displaced
                            # a moment ago cannot replace the live one's.
                            _endpoint_reports[node_id] = _EndpointReport(
                                ws=ws,
                                external_endpoint=upd.external_endpoint,
                                local_endpoints=tuple(upd.local_endpoints),
                            )

                    db_node.last_endpoint = upd.external_endpoint
                    db_node.last_seen_at = datetime.now(timezone.utc)
                    await db.commit()
                    # Broadcast as 'updated' so peers can refresh.
                    await _broadcast_to_household(
                        household_id,
                        SignalRosterUpdate(
                            updated=[_peer_to_enrolled(db_node)]
                        ).model_dump(mode="json"),
                        db=db,
                        except_node_id=node_id,
                    )
                continue

            if msg_type == "request_punch":
                try:
                    rp = SignalRequestPunch.model_validate(raw)
                except ValidationError as e:
                    await _safe_send(
                        ws,
                        SignalError(
                            code="request_punch_invalid",
                            message=f"{e.error_count()} error(s)",
                        ).model_dump(mode="json"),
                    )
                    continue

                async with get_session_factory()() as db:
                    target = await db.get(MeshNode, rp.target_node_id)
                    if target is None or target.household_id != household_id:
                        await _safe_send(
                            ws,
                            SignalError(
                                code="punch_target_unknown",
                                message="target not in your household",
                            ).model_dump(mode="json"),
                        )
                        continue

                    # Look up the requesting peer's current state (we
                    # need its endpoint to send to the responder).
                    requester = await db.get(MeshNode, node_id)
                    if requester is None:
                        continue

                async with _signal_lock:
                    target_ws = _signal_connections.get(rp.target_node_id)

                if target_ws is None:
                    await _safe_send(
                        ws,
                        SignalError(
                            code="punch_target_offline",
                            message="target peer is not currently connected",
                        ).model_dump(mode="json"),
                    )
                    continue

                # Tell the initiator about the target. `safe_endpoint_or_none`
                # guards against a legacy row (persisted before endpoint
                # validation shipped) tripping SignalPunchCoord's own
                # field_validator and killing this WS loop over one bad row -
                # coerce to None (peer just doesn't get a punch hint) instead.
                await _safe_send(
                    ws,
                    SignalPunchCoord(
                        peer_node_id=rp.target_node_id,
                        peer_wg_public_key=target.wg_public_key,
                        peer_endpoint=safe_endpoint_or_none(target.last_endpoint),
                        role="initiator",
                    ).model_dump(mode="json"),
                )
                # Tell the responder to expect a punch from the initiator.
                await _safe_send(
                    target_ws,
                    SignalPunchCoord(
                        peer_node_id=node_id,
                        peer_wg_public_key=requester.wg_public_key,
                        peer_endpoint=safe_endpoint_or_none(requester.last_endpoint),
                        role="responder",
                    ).model_dump(mode="json"),
                )
                continue

            if msg_type == "circle_peer_ack":
                # A sidecar's answer to a circle_peer_add / _extend. Only wakes
                # the create or renew that is waiting for it. Wrapped so nothing
                # an ack carries can end this loop.
                if not token_bound:
                    # No add or extend is ever sent to this connection, so an
                    # ack on it answers nothing the coordinator asked. Ignored,
                    # like any message type it does not act on.
                    logger.debug("circle_link_ack_from_legacy_connection", node_id=str(node_id))
                    continue
                try:
                    await circle_links.handle_ack(node_id, raw)
                except Exception:
                    logger.exception("circle_link_ack_failed", node_id=str(node_id))
                continue

            # Unknown message types are non-fatal - log + ignore so
            # forward-compatible protocol additions don't break old
            # coordinators.
            logger.info(
                "mesh_signal_unknown_msg",
                node_id=str(node_id),
                msg_type=msg_type,
            )

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("mesh_signal_loop_error", node_id=str(node_id))

    # ---- 5. Cleanup + broadcast removed ---- #
    async with _signal_lock:
        current = _signal_connections.get(node_id)
        if current is ws:
            _signal_connections.pop(node_id, None)
            # Drop the node's LAN candidates: they're connection-scoped and
            # meaningless once it's gone. Only clear if this ws is the live one
            # (a newer reconnection may have already replaced it).
            _local_endpoints.pop(node_id, None)
            _signal_features.pop(node_id, None)
            _endpoint_reports.pop(node_id, None)
        # A newer connection for this node is live: this one was displaced. The
        # node has not left, so its household is not told it has.
        displaced = current is not None and current is not ws

    if not displaced:
        try:
            async with get_session_factory()() as db:
                await _broadcast_to_household(
                    household_id,
                    SignalRosterUpdate(removed=[node_id]).model_dump(mode="json"),
                    db=db,
                    except_node_id=node_id,
                )
        except Exception:
            logger.exception("mesh_signal_disconnect_broadcast_failed", node_id=str(node_id))

    logger.info(
        "mesh_signal_disconnected",
        node_id=str(node_id),
        household_id=str(household_id),
        displaced=displaced,
    )
