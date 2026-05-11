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

Signaling WebSocket (`/mesh/signal`) is week-2 work and is deliberately
not included here yet.

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

import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db
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
)
from coordinator.services.household_allocator import (
    HouseholdCIDRExhausted,
    allocate_household_cidr,
)
from coordinator.services.mesh_allocator import MeshIPExhausted, allocate_mesh_ip

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
