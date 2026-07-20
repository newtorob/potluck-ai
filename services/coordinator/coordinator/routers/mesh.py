"""Mesh control-plane endpoints.

This is the coordinator surface area for Potluck's own WireGuard mesh
(per `docs/phase-g/mesh-design.md`). Owns:

  * POST /mesh/households       — create the calling member's household
                                  (idempotent for the dev-stub member).
  * POST /mesh/pairing-tokens   — issue a one-time enrollment token for
                                  the member's household.
  * POST /mesh/enroll           — redeem a pairing token, get a mesh IP
                                  + initial peer roster.
  * GET  /mesh/peers            — current household roster for an
                                  enrolled node (auth via wg_public_key).
  * POST /mesh/heartbeat        — node alive + last-endpoint update.
  * WS   /mesh/signal           — long-lived signaling channel. Carries
                                  endpoint updates + roster broadcasts +
                                  hole-punch coordination between peers.

Privacy posture:
  * The coordinator KNOWS member identity + household membership. That's
    intentional — household scoping requires it.
  * The coordinator NEVER carries this identity through to a contributor
    peer in a JobAssignedMessage. That invariant lives in `routers/jobs.py`
    and is tested at the schema level (test_privacy_invariants.py).
  * Mesh enrollment payload contains only mesh-layer data: pairing token,
    WG pubkey, node name, platform string. NO member_id field is needed
    at this endpoint because the pairing token already encodes household
    membership.
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, WebSocket, WebSocketDisconnect, status
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
    DERPTokenRequest,
    DERPTokenResponse,
    EnrolledPeer,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    HouseholdCreateRequest,
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
from coordinator.middleware.auth import (
    coordinator_token_matches,
    require_coordinator_token,
)
from coordinator.middleware.session_auth import resolve_session_member_id
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
# EnrollResponse without a DB column. Deliberately NOT persisted: LAN
# endpoints are volatile (DHCP, interface churn) and are only useful while
# a node is connected. Single-process, like _signal_connections; a multi-
# process deployment would lift this into shared state. Cleared on disconnect.
_local_endpoints: dict[uuid.UUID, list[str]] = {}

# Lock guarding `_signal_connections` so concurrent welcome / disconnect
# / broadcast operations don't race on dict mutation. Also guards
# `_local_endpoints`.
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


# ---------------------------------------------------------------------------
# POST /mesh/households
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/households",
    response_model=HouseholdResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_coordinator_token)],
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
    POTLUCK_SESSION_AUTH_ENABLED is set, then the logged-in member."""
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
# POST /mesh/pairing-tokens
# ---------------------------------------------------------------------------


@router.post(
    "/mesh/pairing-tokens",
    response_model=PairingTokenIssueResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_coordinator_token)],
)
async def issue_pairing_token(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> PairingTokenIssueResponse:
    """Issue a fresh single-use pairing token for the calling member's
    household. Returns 404 if the member has no household yet (must
    POST /mesh/households first).

    The plaintext token value is returned ONLY in this response. The
    coordinator stores it as the primary key in `pairing_tokens`; if
    the user loses the value they need to issue a new one (rather than
    look it up server-side)."""
    household_result = await db.execute(
        select(Household).where(Household.owner_member_id == member_id)
    )
    household = household_result.scalar_one_or_none()
    if household is None:
        raise HTTPException(
            status_code=404,
            detail="no household for caller — POST /mesh/households first",
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
    dependencies=[Depends(require_coordinator_token)],
)
async def enroll(
    req: EnrollRequest,
    db: AsyncSession = Depends(get_db),
) -> EnrollResponse:
    """Redeem a pairing token, allocate a mesh IP, persist the node,
    return the household's roster.

    Flow:
      1. Look up the pairing token. Must exist, must be unconsumed,
         must not be expired.
      2. Load the household.
      3. Allocate a mesh IP from the household's CIDR.
      4. Create the mesh_nodes row.
      5. Mark the pairing_token consumed.
      6. Return the new node's mesh IP + the household roster.

    On any failure that's the caller's fault → 4xx with a categorical
    reason. Internal errors → 5xx with a generic message; structured
    logs carry the detail.
    """
    now = datetime.now(timezone.utc)

    # ---- 1. Verify the pairing token ---- #
    token_row = await db.get(PairingToken, req.pairing_token)
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
        # data-integrity bug, not a client error — but the client can't
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
    # OPTIONAL — absent for any mesh client that predates DERP-token PoP
    # hardening (back-compat). If present, validate it's actually a decodable
    # Ed25519 key now rather than storing garbage that would just fail every
    # future derp-token verification silently.
    if req.signing_public_key is not None:
        try:
            decode_ed25519_public_key(req.signing_public_key, field_name="signing_public_key")
        except KeyDecodeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---- 5. Persist the new node ---- #
    node = MeshNode(
        household_id=household.id,
        node_name=req.node_name,
        mesh_ip=mesh_ip,
        wg_public_key=req.wg_public_key,
        signing_public_key=req.signing_public_key,
        platform=req.platform,
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
    )


# ---------------------------------------------------------------------------
# GET /mesh/peers
# ---------------------------------------------------------------------------


@router.get(
    "/mesh/peers",
    response_model=list[EnrolledPeer],
    dependencies=[Depends(require_coordinator_token)],
)
async def list_peers(
    wg_public_key: str,
    db: AsyncSession = Depends(get_db),
) -> list[EnrolledPeer]:
    """Return the household roster for a node, identified by its
    `wg_public_key` query parameter.

    Gated by the coordinator token (mirrors every sibling /mesh/* route) so
    an internet-facing coordinator can't be enumerated by anyone who merely
    knows/guesses a registered wg_public_key. This was previously missing —
    LAUNCH-BLOCKER fix.

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
    dependencies=[Depends(require_coordinator_token)],
)
async def delete_mesh_node(
    node_id: uuid.UUID,
    wg_public_key: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a node from a household. Idempotent — returns 204 even if the
    node row is already gone (re-running the cleanup is safe).

    v0 auth model: the caller proves household membership with their own
    wg_public_key. Either:
      * the caller IS the node being deleted (self-leave), OR
      * the caller is a household-mate of the target (peer cleanup of a
        stale roster entry — e.g. a paired machine that died or was
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

    target = await db.get(MeshNode, node_id)
    if target is None:
        # Already gone — return 204 anyway. Idempotent cleanup is more
        # ergonomic than forcing the client to special-case 404.
        return Response(status_code=204)

    if target.household_id != caller.household_id:
        # Don't leak whether the node exists across households — return 403
        # like any other unauthorized delete.
        raise HTTPException(
            status_code=403, detail="caller is not in target's household"
        )

    target_id_str = str(target.id)
    household_id = target.household_id

    await db.delete(target)
    await db.commit()

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
    dependencies=[Depends(require_coordinator_token)],
)
async def heartbeat(
    req: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    """Update a node's `last_endpoint` + `last_seen_at`.

    Same v0 auth model as /mesh/peers: the (node_id, wg_public_key)
    pair must match an existing row. v1 will move to a signed-nonce
    flow.
    """
    node = await db.get(MeshNode, req.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    if node.wg_public_key != req.wg_public_key:
        # Identifying as a different node than the caller's key matches
        # → reject with 403, not 404, to make the failure mode clear.
        raise HTTPException(status_code=403, detail="wg_public_key mismatch")

    node.last_endpoint = req.external_endpoint
    node.last_seen_at = datetime.now(timezone.utc)
    await db.commit()

    return HeartbeatResponse(server_time=node.last_seen_at)


# ---------------------------------------------------------------------------
# POST /mesh/derp-token
# ---------------------------------------------------------------------------


def _verify_derp_pop(node: MeshNode, req: DERPTokenRequest) -> None:
    """Verify the OPTIONAL proof-of-possession signature on a DERP-token
    request. Raises HTTPException on any failure. No-op (returns silently)
    when the request carries no signature and PoP isn't required — the
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
        # falling back to the legacy check — the caller explicitly asked for
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
    dependencies=[Depends(require_coordinator_token)],
)
async def issue_derp_token(
    req: DERPTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> DERPTokenResponse:
    """Issue a short-lived JWT that authorises a mesh node to connect to
    the DERP relay.

    Auth model: possession of a registered wg_public_key is the BASE
    credential — the coordinator verifies the key exists in mesh_nodes. This
    alone is weak: wg_public_key is not secret (nodes exchange it with peers
    for the WG handshake), so anyone who has observed it can mint a token AS
    that node. See docs/security audit HIGH finding on DERP-token PoP.

    v0.5 hardening (back-compat, additive — see DERPTokenRequest's
    docstring): OPTIONAL `nonce` + `signature` prove the caller holds the
    private key for a separately-registered `MeshNode.signing_public_key`
    (Ed25519 — distinct from the WG/Curve25519 key, which isn't usable for
    signature verification). When both are present they are ALWAYS verified
    (mismatch -> 401, regardless of `derp_pop_required`). When absent:
      * `derp_pop_required=False` (default) — legacy behaviour: the
        wg_public_key-possession check alone is accepted, and we log a
        warning so operators can see how much of their fleet hasn't
        upgraded. Needed because the current Go mesh client
        (coordapi/client.go) does not yet sign this challenge — flipping
        the flag on before that ships would lock out every enrolled node.
      * `derp_pop_required=True` — the request is rejected (401). Only
        safe once the Go client signs by default.

    Privacy contract:
      * JWT claims: wg_pubkey + region + exp + iat ONLY.
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
            reason="caller omitted nonce/signature — accepted under legacy "
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

    # Log the truncated pubkey only — never the token or full claims.
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
    # to talk to. Note this queries by household_id only — connections
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
                # Only evict if the registry still holds THIS ws — a peer
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
         and broadcasts the node's `removed` to the household.

    v0 auth: a coordinator token (when configured) gates the WS upgrade, then
    possession of a valid `wg_public_key` identifies the node. Stronger auth
    (signed-nonce member<->key challenge mirroring /peers/connect) is a v0.5
    hardening item.
    """
    await ws.accept()

    # ---- coordinator-token gate (mirrors /peers/connect) ---- #
    # No-op when POTLUCK_COORDINATOR_TOKEN is unset (tokenless household
    # coordinators unaffected). When configured, the mesh sidecar must present
    # X-Potluck-Coordinator-Token on the WS upgrade. Raises the bar for joining
    # the signaling channel from "anyone with any enrolled WG pubkey" (which the
    # code's own comments call not-secret) to "anyone holding the coordinator
    # token", while the full signed-nonce member<->key binding is built (v0.5).
    if not coordinator_token_matches(ws.headers.get("x-potluck-coordinator-token")):
        logger.info("mesh_signal_rejected_bad_coordinator_token")
        await _safe_send(
            ws,
            SignalError(
                code="unauthorized", message="coordinator token required"
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
    async with _signal_lock:
        old_ws = _signal_connections.get(node_id)
        _signal_connections[node_id] = ws

    if old_ws is not None:
        # A previous connection for this node existed — close it cleanly.
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
    )

    # ---- 4. Message loop ---- #
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

                # Store the last-reported LAN candidates in-memory (already
                # filtered to private ip:port + capped by the schema validator)
                # so they flow into roster snapshots via _peer_to_enrolled.
                # No DB write — these are volatile and connection-scoped.
                async with _signal_lock:
                    _local_endpoints[node_id] = list(upd.local_endpoints)

                async with get_session_factory()() as db:
                    db_node = await db.get(MeshNode, node_id)
                    if db_node is not None:
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
                # field_validator and killing this WS loop over one bad row —
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

            # Unknown message types are non-fatal — log + ignore so
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
            # meaningless once it's gone. Only clear if THIS ws is the live one
            # (a newer reconnection may have already replaced it).
            _local_endpoints.pop(node_id, None)

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
    )
