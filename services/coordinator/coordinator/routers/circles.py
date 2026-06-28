"""Trusted-circle CRUD endpoints + own-identity disclosure.

A trusted circle is a named allow-list of cooperative member pubkeys owned by
one member.  Routing scope 'circle' restricts dispatch to peers whose member's
pubkey is in the chosen circle.  Ownership is the only access-control gate: a
member reads and writes only their own circles; the owner_member_id filter is
applied on every query without exception.

v0/dev: member identity comes from _STUB_MEMBER_ID (same pattern as me.py and
jobs.py). Real member auth swaps in here when v0.5 lands.

Privacy: circle contents (member pubkeys) are visible only to the circle owner.
No circle endpoint leaks another member's identity into its response beyond the
pubkeys the owner themselves added.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.db.database import get_db
from coordinator.middleware.auth import require_coordinator_token
from coordinator.models.circle import CircleMember, TrustedCircle
from coordinator.models.member_key import MemberKey

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["circles"])

_STUB_MEMBER_ID = STUB_MEMBER_ID

# Plausible key string bounds: non-empty, at most 512 chars (matches the
# member_keys.public_key column; Ed25519 base64url is ~43 chars but we don't
# over-validate format — accept anything in this range).
_PUBKEY_MAX_LEN = 512


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class CircleMemberOut(BaseModel):
    member_pubkey: str
    added_at: datetime


class CircleOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    members: list[CircleMemberOut]


class CircleListResponse(BaseModel):
    circles: list[CircleOut]


class CreateCircleRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        if len(v) > 128:
            raise ValueError("name must be at most 128 characters")
        return v


class AddCircleMemberRequest(BaseModel):
    member_pubkey: str

    @field_validator("member_pubkey")
    @classmethod
    def pubkey_plausible(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("member_pubkey must not be empty")
        if len(v) > _PUBKEY_MAX_LEN:
            raise ValueError(f"member_pubkey must be at most {_PUBKEY_MAX_LEN} characters")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_own_circle(
    db: AsyncSession, circle_id: uuid.UUID
) -> TrustedCircle:
    """Fetch a circle that belongs to the calling member, or 404."""
    row = await db.get(TrustedCircle, circle_id)
    if row is None or row.owner_member_id != _STUB_MEMBER_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="circle not found")
    return row


async def _load_circle_members(
    db: AsyncSession, circle_id: uuid.UUID
) -> list[CircleMemberOut]:
    result = await db.execute(
        select(CircleMember).where(CircleMember.circle_id == circle_id)
        .order_by(CircleMember.added_at)
    )
    return [
        CircleMemberOut(member_pubkey=m.member_pubkey, added_at=m.added_at)
        for m in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/me/circles",
    response_model=CircleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_coordinator_token)],
)
async def create_circle(
    req: CreateCircleRequest,
    db: AsyncSession = Depends(get_db),
) -> CircleOut:
    """Create a new named trusted circle owned by the calling member."""
    circle = TrustedCircle(owner_member_id=_STUB_MEMBER_ID, name=req.name)
    db.add(circle)
    await db.flush()
    await db.commit()
    logger.info("circle_created", circle_id=str(circle.id), name=req.name)
    return CircleOut(id=circle.id, name=circle.name, created_at=circle.created_at, members=[])


@router.get(
    "/me/circles",
    response_model=CircleListResponse,
    dependencies=[Depends(require_coordinator_token)],
)
async def list_circles(
    db: AsyncSession = Depends(get_db),
) -> CircleListResponse:
    """List all circles owned by the calling member, with their members."""
    result = await db.execute(
        select(TrustedCircle)
        .where(TrustedCircle.owner_member_id == _STUB_MEMBER_ID)
        .order_by(TrustedCircle.created_at)
    )
    circles = result.scalars().all()

    out: list[CircleOut] = []
    for c in circles:
        members = await _load_circle_members(db, c.id)
        out.append(CircleOut(id=c.id, name=c.name, created_at=c.created_at, members=members))

    return CircleListResponse(circles=out)


@router.delete(
    "/me/circles/{circle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_coordinator_token)],
)
async def delete_circle(
    circle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a circle and all its members. 404 if not owned by caller."""
    circle = await _get_own_circle(db, circle_id)
    # CASCADE on FK takes care of circle_members rows.
    await db.delete(circle)
    await db.commit()
    logger.info("circle_deleted", circle_id=str(circle_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/circles/{circle_id}/members",
    response_model=CircleMemberOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_coordinator_token)],
)
async def add_circle_member(
    circle_id: uuid.UUID,
    req: AddCircleMemberRequest,
    db: AsyncSession = Depends(get_db),
) -> CircleMemberOut:
    """Add a member pubkey to the caller's circle.

    Returns 404 if the circle doesn't belong to the caller, 409 if the
    pubkey is already in the circle.
    """
    await _get_own_circle(db, circle_id)
    entry = CircleMember(circle_id=circle_id, member_pubkey=req.member_pubkey)
    db.add(entry)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="member_pubkey is already in this circle",
        )
    logger.info(
        "circle_member_added",
        circle_id=str(circle_id),
        pubkey_prefix=req.member_pubkey[:16],
    )
    return CircleMemberOut(member_pubkey=entry.member_pubkey, added_at=entry.added_at)


@router.delete(
    "/me/circles/{circle_id}/members/{member_pubkey:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_coordinator_token)],
)
async def remove_circle_member(
    circle_id: uuid.UUID,
    member_pubkey: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a member pubkey from the caller's circle. 204 whether or not it was present."""
    await _get_own_circle(db, circle_id)
    await db.execute(
        delete(CircleMember).where(
            CircleMember.circle_id == circle_id,
            CircleMember.member_pubkey == member_pubkey,
        )
    )
    await db.commit()
    logger.info(
        "circle_member_removed",
        circle_id=str(circle_id),
        pubkey_prefix=member_pubkey[:16],
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Own-identity disclosure — "your key to share"
# ---------------------------------------------------------------------------


@router.get(
    "/me/identity",
    dependencies=[Depends(require_coordinator_token)],
)
async def my_identity(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the calling member's own public key so they can share it with
    others for circle membership.

    Reads the most-recently-added key bound to this member from member_keys.
    Returns 404 if no key has been registered yet (the member hasn't
    enrolled a device).

    Privacy: this endpoint is self-service only — a member reads only their
    own pubkey, never another member's.
    """
    result = await db.execute(
        select(MemberKey.public_key)
        .where(MemberKey.member_id == _STUB_MEMBER_ID)
        .order_by(MemberKey.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No device key registered for this member yet.",
        )
    return {"member_pubkey": row}
