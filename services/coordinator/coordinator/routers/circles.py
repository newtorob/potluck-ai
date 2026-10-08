"""Trusted circles: create, invite, join, leave, and own-identity disclosure.

A trusted circle is a named group of members who lend each other spare
compute. Routing scope 'circle' restricts dispatch to machines of the
circle's other members.

Someone is in a circle in one of two ways:

* They hold a membership: the owner from creation, anyone else by accepting
  an invite the owner made. Members see the circle, borrow from each other,
  and can leave.
* An owner using an older app pasted in their machine key. That member lends
  as before, but holds no membership and cannot borrow.

Owner-only: rename-free management (delete, invite, remove a person, the key
list). Member-level: list, availability, borrowing, leaving.

Member identity comes from the caller's session via
``resolve_session_member_id`` (same pattern as me.py and jobs.py). In dev
(``POTLUCK_SESSION_AUTH_ENABLED`` unset) that resolves to the fixture
STUB_MEMBER_ID; with auth enabled it is the logged-in member.

Privacy: a circle's people are visible to its members and to nobody else,
by the name they set or their sign-in email. Requests are never attributed
to a person here. People are addressed by their membership ID, which means
nothing outside the circle; account IDs are not returned.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.crypto import KeyDecodeError, decode_ed25519_public_key
from coordinator.db.database import get_db
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models.circle import CircleInvite, CircleMember, CircleMembership, TrustedCircle
from coordinator.models.member_key import MemberKey
from coordinator.services.circles import (
    MAX_CIRCLE_PEOPLE,
    hash_invite_code,
    lender_member_ids,
    member_names,
    membership_of,
    new_invite_code,
    normalise_invite_code,
)

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


class CirclePersonOut(BaseModel):
    """Someone in the circle, as its members see them."""

    id: uuid.UUID  # the membership, meaningful only inside this circle
    name: str
    role: str  # "owner" | "member"
    is_you: bool
    joined_at: datetime


class CircleOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    # The caller's own place in the circle.
    role: str = "owner"
    # Everyone holding a membership, owner first.
    people: list[CirclePersonOut] = []
    # Machine keys the owner pasted in with an older app. Owner only.
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


async def _get_member_circle(
    db: AsyncSession, circle_id: uuid.UUID, member_id: uuid.UUID
) -> tuple[TrustedCircle, CircleMembership]:
    """Fetch a circle the caller is in, with their membership, or 404. Not
    being in it and it not existing look the same."""
    row = await db.get(TrustedCircle, circle_id)
    mine = await membership_of(db, circle_id, member_id) if row is not None else None
    if row is None or mine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="circle not found")
    return row, mine


async def _load_people(
    db: AsyncSession, circle_id: uuid.UUID, viewer: uuid.UUID
) -> list[CirclePersonOut]:
    rows = (await db.execute(
        select(CircleMembership).where(CircleMembership.circle_id == circle_id)
        .order_by(CircleMembership.joined_at)
    )).scalars().all()
    names = await member_names(db, {r.member_id for r in rows})
    people = [
        CirclePersonOut(
            id=r.id,
            name=names.get(r.member_id) or "Potluck member",
            role=r.role,
            is_you=r.member_id == viewer,
            joined_at=r.joined_at,
        )
        for r in rows
    ]
    people.sort(key=lambda p: (p.role != "owner", p.joined_at))
    return people


async def _circle_out(db: AsyncSession, circle: TrustedCircle, viewer: uuid.UUID, role: str) -> CircleOut:
    return CircleOut(
        id=circle.id,
        name=circle.name,
        created_at=circle.created_at,
        role=role,
        people=await _load_people(db, circle.id, viewer),
        members=await _load_circle_members(db, circle.id) if role == "owner" else [],
    )


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
    db.add(CircleMembership(circle_id=circle.id, member_id=member_id, role="owner"))
    await db.flush()
    out = await _circle_out(db, circle, member_id, "owner")
    await db.commit()
    logger.info("circle_created", circle_id=str(circle.id))
    return out


@router.get(
    "/me/circles",
    response_model=CircleListResponse,
)
async def list_circles(
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleListResponse:
    """List every circle the calling member is in: the ones they own and
    the ones they joined, each with its people and the caller's role."""
    result = await db.execute(
        select(TrustedCircle, CircleMembership.role)
        .join(CircleMembership, CircleMembership.circle_id == TrustedCircle.id)
        .where(CircleMembership.member_id == member_id)
        .order_by(TrustedCircle.created_at)
    )
    out = [await _circle_out(db, circle, member_id, role) for circle, role in result.all()]
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

    Returns 404 if the circle doesn't belong to the caller, 400 if the value
    is not a machine key at all, 409 if the pubkey is already in the circle.
    """
    await _get_own_circle(db, circle_id, member_id)
    # A mistyped or truncated key used to be stored and then never routed to,
    # with nothing to tell the owner why their friend's machine never answered.
    try:
        decode_ed25519_public_key(req.member_pubkey, field_name="member_pubkey")
    except KeyDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not a Potluck machine key. Ask them to copy it again from Trusted circles.",
        )
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
# What a circle can run right now
# ---------------------------------------------------------------------------


class CircleModelOut(BaseModel):
    slug: str
    machines: int


class CircleAvailabilityOut(BaseModel):
    circle_id: uuid.UUID
    models: list[CircleModelOut]


@router.get(
    "/me/circles/{circle_id}/availability",
    response_model=CircleAvailabilityOut,
)
async def circle_availability(
    circle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleAvailabilityOut:
    """The models a circle can answer the caller with at this moment, and how
    many machines offer each: the other members' machines that are online
    and sharing with circles. An empty list means none is. 404 unless the
    caller is in the circle.

    Privacy: counts per model only. No machine, member or key is named.
    """
    from coordinator.services.peer_registry import circle_model_availability

    await _get_member_circle(db, circle_id, member_id)
    counts = await circle_model_availability(db, await lender_member_ids(db, circle_id, member_id))
    models = [
        CircleModelOut(slug=slug, machines=n)
        for slug, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return CircleAvailabilityOut(circle_id=circle_id, models=models)


# ---------------------------------------------------------------------------
# Invites: how someone joins
# ---------------------------------------------------------------------------


class CreateInviteRequest(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)
    max_uses: int = Field(default=1, ge=1, le=20)


class InviteCreatedOut(BaseModel):
    id: uuid.UUID
    # Shown once. Only its hash is stored.
    code: str
    url: str
    expires_at: datetime
    max_uses: int


class InviteOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    max_uses: int
    uses: int


class InvitePreviewOut(BaseModel):
    circle_name: str
    invited_by: str
    people: int
    expires_at: datetime
    already_in: bool


class AcceptInviteRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=512)


# The code rides in the fragment, which a browser never sends to the server,
# so opening the link does not put the code in a web server's logs.
INVITE_URL = "https://trypotluck.ai/circles/join"


def _usable(now: datetime):
    """SQL condition for an invite that can still be accepted."""
    return (
        CircleInvite.revoked_at.is_(None),
        CircleInvite.expires_at > now,
        CircleInvite.uses < CircleInvite.max_uses,
    )


async def _invite_for_code(db: AsyncSession, raw_code: str) -> CircleInvite:
    """The usable invite a pasted code or link names, or 404. A wrong code,
    an expired one, a revoked one and a used-up one all look the same."""
    canonical = normalise_invite_code(raw_code)
    invite = None
    if canonical is not None:
        invite = (await db.execute(
            select(CircleInvite).where(
                CircleInvite.code_hash == hash_invite_code(canonical),
                *_usable(datetime.now(timezone.utc)),
            )
        )).scalar_one_or_none()
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That invite is not valid. It may have expired or already been used. Ask for a new one.",
        )
    return invite


@router.post(
    "/me/circles/{circle_id}/invites",
    response_model=InviteCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    circle_id: uuid.UUID,
    req: CreateInviteRequest | None = None,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> InviteCreatedOut:
    """Make an invite to the caller's circle. The code is returned once."""
    req = req or CreateInviteRequest()
    await _get_own_circle(db, circle_id, member_id)
    code = new_invite_code()
    invite = CircleInvite(
        circle_id=circle_id,
        code_hash=hash_invite_code(normalise_invite_code(code) or ""),
        created_by=member_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=req.expires_in_days),
        max_uses=req.max_uses,
    )
    db.add(invite)
    await db.flush()
    await db.commit()
    logger.info("circle_invite_created", circle_id=str(circle_id), invite_id=str(invite.id))
    return InviteCreatedOut(
        id=invite.id, code=code, url=f"{INVITE_URL}#code={code}",
        expires_at=invite.expires_at, max_uses=invite.max_uses,
    )


@router.get("/me/circles/{circle_id}/invites", response_model=list[InviteOut])
async def list_invites(
    circle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> list[InviteOut]:
    """The caller's circle's invites that can still be accepted. Never the codes."""
    await _get_own_circle(db, circle_id, member_id)
    rows = (await db.execute(
        select(CircleInvite).where(
            CircleInvite.circle_id == circle_id, *_usable(datetime.now(timezone.utc))
        ).order_by(CircleInvite.created_at)
    )).scalars().all()
    return [InviteOut(id=r.id, created_at=r.created_at, expires_at=r.expires_at,
                      max_uses=r.max_uses, uses=r.uses) for r in rows]


@router.delete(
    "/me/circles/{circle_id}/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_invite(
    circle_id: uuid.UUID,
    invite_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Withdraw an invite. People who already joined with it stay."""
    await _get_own_circle(db, circle_id, member_id)
    await db.execute(
        update(CircleInvite)
        .where(CircleInvite.id == invite_id, CircleInvite.circle_id == circle_id,
               CircleInvite.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/circles/invites/preview", response_model=InvitePreviewOut)
async def preview_invite(
    req: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> InvitePreviewOut:
    """What an invite is for, before accepting it: the circle's name, who
    made the invite and how many people are in it. The code travels in the
    body, never in a URL."""
    invite = await _invite_for_code(db, req.code)
    circle = await db.get(TrustedCircle, invite.circle_id)
    people = await db.scalar(
        select(func.count()).select_from(CircleMembership)
        .where(CircleMembership.circle_id == invite.circle_id)
    )
    names = await member_names(db, {invite.created_by})
    return InvitePreviewOut(
        circle_name=circle.name,
        invited_by=names.get(invite.created_by) or "A Potluck member",
        people=int(people or 0),
        expires_at=invite.expires_at,
        already_in=await membership_of(db, invite.circle_id, member_id) is not None,
    )


@router.post("/circles/invites/accept", response_model=CircleOut)
async def accept_invite(
    req: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleOut:
    """Join the circle an invite names. This is the consent step: from here
    the circle's members can borrow this member's machines that share with
    circles, and this member can borrow theirs. Accepting again changes
    nothing and does not use the invite up."""
    invite = await _invite_for_code(db, req.code)
    circle = await db.get(TrustedCircle, invite.circle_id)
    existing = await membership_of(db, invite.circle_id, member_id)
    if existing is not None:
        return await _circle_out(db, circle, member_id, existing.role)

    people = await db.scalar(
        select(func.count()).select_from(CircleMembership)
        .where(CircleMembership.circle_id == invite.circle_id)
    )
    if int(people or 0) >= MAX_CIRCLE_PEOPLE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This circle is full.")

    # Claim one use atomically, so two people racing for a single-use invite
    # cannot both get in.
    claimed = (await db.execute(
        update(CircleInvite)
        .where(CircleInvite.id == invite.id, *_usable(datetime.now(timezone.utc)))
        .values(uses=CircleInvite.uses + 1)
        .returning(CircleInvite.id)
    )).scalar_one_or_none()
    if claimed is None:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That invite is not valid. It may have expired or already been used. Ask for a new one.",
        )
    db.add(CircleMembership(circle_id=invite.circle_id, member_id=member_id, role="member"))
    try:
        await db.flush()
    except IntegrityError:
        # Joined in a parallel request: keep that membership, give the use back.
        await db.rollback()
        existing = await membership_of(db, invite.circle_id, member_id)
        circle = await db.get(TrustedCircle, invite.circle_id)
        return await _circle_out(db, circle, member_id, existing.role if existing else "member")
    out = await _circle_out(db, circle, member_id, "member")
    await db.commit()
    logger.info("circle_joined", circle_id=str(invite.circle_id), invite_id=str(invite.id))
    return out


@router.delete("/me/circles/{circle_id}/membership", status_code=status.HTTP_204_NO_CONTENT)
async def leave_circle(
    circle_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Leave a circle. From the next request the circle's members can no
    longer borrow this member's machines, nor this member theirs. The owner
    cannot leave; they delete the circle."""
    _, mine = await _get_member_circle(db, circle_id, member_id)
    if mine.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You own this circle. Delete it to end it for everyone.",
        )
    await db.delete(mine)
    await db.commit()
    logger.info("circle_left", circle_id=str(circle_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/me/circles/{circle_id}/people/{person_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_person(
    circle_id: uuid.UUID,
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """Remove someone from the caller's circle. 204 whether or not they were
    in it. The owner cannot be removed."""
    await _get_own_circle(db, circle_id, member_id)
    await db.execute(
        delete(CircleMembership).where(
            CircleMembership.id == person_id,
            CircleMembership.circle_id == circle_id,
            CircleMembership.role != "owner",
        )
    )
    await db.commit()
    logger.info("circle_person_removed", circle_id=str(circle_id))
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

    Auth: gated by `resolve_session_member_id` only (the real identity
    gate: with `POTLUCK_SESSION_AUTH_ENABLED=1` this 401s without a valid
    `potluck_session` cookie / bearer session token; in dev passthrough it
    resolves to the fixture member). Deliberately NOT gated by
    `require_coordinator_token` — this route is called directly by the
    desktop frontend, which can never hold the coordinator's shared secret
    (see the module docstring's "frontend-direct" invariant).

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
