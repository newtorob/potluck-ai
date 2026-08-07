"""Trusted-circle CRUD endpoints + own-identity disclosure.

A trusted circle is a named allow-list of cooperative member pubkeys owned by
one member.  Routing scope 'circle' restricts dispatch to peers whose member's
pubkey is in the chosen circle.  Ownership is the only access-control gate: a
member reads and writes only their own circles; the owner_member_id filter is
applied on every query without exception.

Member identity comes from the caller's session via
``resolve_session_member_id`` (same pattern as me.py and jobs.py). In dev
(``POTLUCK_SESSION_AUTH_ENABLED`` unset) that resolves to the fixture
STUB_MEMBER_ID; with auth enabled it is the logged-in member.

Privacy: circle contents (member pubkeys) are visible only to the circle owner.
No circle endpoint leaks another member's identity into its response beyond the
pubkeys the owner themselves added.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.crypto import KeyDecodeError, decode_ed25519_public_key
from coordinator.db.database import get_db
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models.circle import CircleMember, TrustedCircle
from coordinator.models.member_key import MemberKey

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["circles"])

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
    db: AsyncSession, circle_id: uuid.UUID, member_id: uuid.UUID
) -> TrustedCircle:
    """Fetch a circle that belongs to the calling member, or 404."""
    row = await db.get(TrustedCircle, circle_id)
    if row is None or row.owner_member_id != member_id:
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
)
async def create_circle(
    req: CreateCircleRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleOut:
    """Create a new named trusted circle owned by the calling member."""
    circle = TrustedCircle(owner_member_id=member_id, name=req.name)
    db.add(circle)
    await db.flush()
    await db.commit()
    logger.info("circle_created", circle_id=str(circle.id), name=req.name)
    return CircleOut(id=circle.id, name=circle.name, created_at=circle.created_at, members=[])


@router.get(
    "/me/circles",
    response_model=CircleListResponse,
)
async def list_circles(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleListResponse:
    """List all circles owned by the calling member, with their members."""
    result = await db.execute(
        select(TrustedCircle)
        .where(TrustedCircle.owner_member_id == member_id)
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
)
async def delete_circle(
    circle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Delete a circle and all its members. 404 if not owned by caller."""
    circle = await _get_own_circle(db, circle_id, member_id)
    # CASCADE on FK takes care of circle_members rows.
    await db.delete(circle)
    await db.commit()
    logger.info("circle_deleted", circle_id=str(circle_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/circles/{circle_id}/members",
    response_model=CircleMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_circle_member(
    circle_id: uuid.UUID,
    req: AddCircleMemberRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleMemberOut:
    """Add a member pubkey to the caller's circle.

    Returns 404 if the circle doesn't belong to the caller, 409 if the
    pubkey is already in the circle.
    """
    await _get_own_circle(db, circle_id, member_id)
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
)
async def remove_circle_member(
    circle_id: uuid.UUID,
    member_pubkey: str,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Remove a member pubkey from the caller's circle. 204 whether or not it was present."""
    await _get_own_circle(db, circle_id, member_id)
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
)
async def my_identity(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
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
        .where(MemberKey.member_id == member_id)
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


# ---------------------------------------------------------------------------
# Device-key registration — v0.5 attestation onboarding path
# ---------------------------------------------------------------------------
#
# `models/member_key.py` has documented since it landed that "v0.5 member-auth
# replaces TOFU: a signed-in member registers their device key through the
# authenticated session (added_via='session')" — but no route ever implemented
# it. That's a real gap: with `require_registered_keys=true` (the strict
# allowlist posture `services/attestation.py` expects for multi-member /
# production coordinators), an unregistered device key hard-fails attestation
# with no way to ever register one, since MemberKey rows were otherwise only
# created via TOFU (disabled in strict mode), operator SQL backfill, or a seed
# script. That's an onboarding trap that pressures an operator into disabling
# `require_registered_keys` — this endpoint is the promised fix.


class DeviceKeyRegisterRequest(BaseModel):
    """Body of POST /me/device-keys.

    `public_key` is the device's Ed25519 public key (base64url, no padding) —
    the same key the desktop sidecar will later present as `peer_public_key`
    in its WS attestation handshake. This endpoint does NOT verify possession
    of the matching private key (that proof happens at attestation time via
    the signed-nonce challenge in `services/attestation.verify_attestation`);
    it only pre-authorizes the key for the authenticated member, which is what
    lets a brand-new device attest at all once `require_registered_keys` is
    enabled.
    """

    public_key: str = Field(..., min_length=1, max_length=_PUBKEY_MAX_LEN)
    label: str | None = Field(default=None, max_length=256)

    @field_validator("label")
    @classmethod
    def _label_or_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class DeviceKeyOut(BaseModel):
    public_key: str
    member_id: uuid.UUID
    label: str | None
    added_via: str
    created_at: datetime


@router.post(
    "/me/device-keys",
    response_model=DeviceKeyOut,
    status_code=status.HTTP_201_CREATED,
)
async def register_device_key(
    req: DeviceKeyRegisterRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> DeviceKeyOut:
    """Register a device Ed25519 public key to the calling (session-
    authenticated) member.

    Auth: gated the same way as every other `/me/*` route —
    `require_coordinator_token` (bearer-token transport gate) plus
    `resolve_session_member_id` (the real identity gate: with
    `POTLUCK_SESSION_AUTH_ENABLED=1` this 401s without a valid
    `potluck_session` cookie / bearer session token; in dev passthrough it
    resolves to the fixture member, matching every other `/me/*` endpoint).

    Validates `public_key` decodes to a 32-byte Ed25519 key (same check the
    WS attestation handshake applies), then inserts a `member_keys` row with
    `added_via='session'`. Returns 409 if the key is already registered — to
    this member or to a different one; we don't disclose which, so this
    endpoint can't be used to fingerprint whether a guessed key belongs to
    someone else.

    This endpoint only pre-authorizes the key; it is NOT the possession
    proof. `services/attestation.verify_attestation` still requires the
    peer to sign the server's connection nonce with the matching private key
    before it can attest as this member — registering a key here does not by
    itself let anyone impersonate the device.
    """
    try:
        decode_ed25519_public_key(req.public_key)
    except KeyDecodeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    entry = MemberKey(
        public_key=req.public_key,
        member_id=member_id,
        label=req.label,
        added_via="session",
    )
    db.add(entry)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="public_key is already registered",
        )

    logger.info(
        "device_key_registered",
        member_id=str(member_id),
        pubkey_prefix=req.public_key[:16],
    )
    return DeviceKeyOut(
        public_key=entry.public_key,
        member_id=entry.member_id,
        label=entry.label,
        added_via=entry.added_via,
        created_at=entry.created_at,
    )
