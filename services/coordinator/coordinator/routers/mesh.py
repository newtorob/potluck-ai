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
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db, get_session_factory
from coordinator.models.household import Household
from coordinator.models.mesh_node import MeshNode
from coordinator.models.pairing import PairingToken
from coordinator.schemas.mesh import (
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
)
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

# Lock guarding `_signal_connections` so concurrent welcome / disconnect
# / broadcast operations don't race on dict mutation.
_signal_lock = asyncio.Lock()

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["mesh"])

# Stub-only constant — real member auth lands with the PBC member-share
# class in v0.5. Until then, every /mesh/households + /mesh/pairing-tokens
# call attributes to this single member. Matches the pattern used in
# `routers/jobs.py:_STUB_MEMBER_ID`.
_STUB_MEMBER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# How long a pairing token stays valid. 1 hour per design doc:
# long enough for cross-room walk-and-install, short enough that a
# leaked token is low-impact.
PAIRING_TOKEN_TTL = timedelta(hours=1)


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
)
async def create_household(
    req: HouseholdCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> HouseholdResponse:
    """Create the dev-stub member's household. Idempotent: if a household
    already exists for the stub member, return it instead of creating
    a duplicate (avoids the UNIQUE-mesh_cidr collision on second call).

    When real member auth lands, the dev-stub bypass goes away and this
    becomes the standard "first-run create-household" call any newly-
    authenticated member makes once."""
    # If the stub member already has a household, return it.
    existing = await db.execute(
        select(Household).where(Household.owner_member_id == _STUB_MEMBER_ID)
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
        owner_member_id=_STUB_MEMBER_ID,
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
)
async def issue_pairing_token(
    db: AsyncSession = Depends(get_db),
) -> PairingTokenIssueResponse:
    """Issue a fresh single-use pairing token for the dev-stub member's
    household. Returns 404 if the member has no household yet (must
    POST /mesh/households first).

    The plaintext token value is returned ONLY in this response. The
    coordinator stores it as the primary key in `pairing_tokens`; if
    the user loses the value they need to issue a new one (rather than
    look it up server-side)."""
    household_result = await db.execute(
        select(Household).where(Household.owner_member_id == _STUB_MEMBER_ID)
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
        issued_by_member_id=_STUB_MEMBER_ID,
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

    # ---- 5. Persist the new node ---- #
    node = MeshNode(
        household_id=household.id,
        node_name=req.node_name,
        mesh_ip=mesh_ip,
        wg_public_key=req.wg_public_key,
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
    peers = [
        EnrolledPeer(
            node_id=peer.id,
            node_name=peer.node_name,
            mesh_ip=str(peer.mesh_ip),
            wg_public_key=peer.wg_public_key,
            last_endpoint=peer.last_endpoint,
            last_seen_at=peer.last_seen_at,
            platform=peer.platform,
        )
        for peer in roster_result.scalars().all()
    ]

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


@router.get("/mesh/peers", response_model=list[EnrolledPeer])
async def list_peers(
    wg_public_key: str,
    db: AsyncSession = Depends(get_db),
) -> list[EnrolledPeer]:
    """Return the household roster for a node, identified by its
    `wg_public_key` query parameter.

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
    return [
        EnrolledPeer(
            node_id=peer.id,
            node_name=peer.node_name,
            mesh_ip=str(peer.mesh_ip),
            wg_public_key=peer.wg_public_key,
            last_endpoint=peer.last_endpoint,
            last_seen_at=peer.last_seen_at,
            platform=peer.platform,
        )
        for peer in roster_result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# POST /mesh/heartbeat
# ---------------------------------------------------------------------------


@router.post("/mesh/heartbeat", response_model=HeartbeatResponse)
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
# WS /mesh/signal
# ---------------------------------------------------------------------------


def _peer_to_enrolled(peer: MeshNode) -> EnrolledPeer:
    return EnrolledPeer(
        node_id=peer.id,
        node_name=peer.node_name,
        mesh_ip=str(peer.mesh_ip),
        wg_public_key=peer.wg_public_key,
        last_endpoint=peer.last_endpoint,
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
    dead: list[uuid.UUID] = []
    for nid, ws in targets:
        ok = await _safe_send(ws, payload)
        if not ok:
            dead.append(nid)
    if dead:
        async with _signal_lock:
            for nid in dead:
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

    v0 auth: possession of a valid `wg_public_key` is the credential.
    Consistent with GET /mesh/peers + POST /mesh/heartbeat. Stronger
    auth (signed-nonce challenge mirroring /peers/connect) is a v1
    hardening item.
    """
    await ws.accept()

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

                # Tell the initiator about the target.
                await _safe_send(
                    ws,
                    SignalPunchCoord(
                        peer_node_id=rp.target_node_id,
                        peer_wg_public_key=target.wg_public_key,
                        peer_endpoint=target.last_endpoint,
                        role="initiator",
                    ).model_dump(mode="json"),
                )
                # Tell the responder to expect a punch from the initiator.
                await _safe_send(
                    target_ws,
                    SignalPunchCoord(
                        peer_node_id=node_id,
                        peer_wg_public_key=requester.wg_public_key,
                        peer_endpoint=requester.last_endpoint,
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
