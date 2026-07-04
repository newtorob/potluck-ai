from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db
from potluck_api.middleware.auth import get_current_member
from potluck_api.models.member import Member
from potluck_api.models.membership import Membership

router = APIRouter(tags=["members"])

# The DB constrains membership status to a Stripe-shaped vocabulary
# ('active'|'past_due'|'canceled'|'incomplete'); the frontend type uses a
# friendlier vocabulary. Map the ones that differ; pass anything else through.
_STATUS_MAP = {"canceled": "cancelled", "past_due": "lapsed"}


class MemberPatch(BaseModel):
    """Fields the member is allowed to update on their own profile."""

    display_name: str | None = None


async def _latest_membership(db: AsyncSession, member_id: object) -> Membership | None:
    """Return the member's most recent membership row, or None."""
    return await db.scalar(
        select(Membership)
        .where(Membership.member_id == member_id)
        .order_by(Membership.created_at.desc())
    )


def _profile(member: Member, membership: Membership | None) -> dict[str, object]:
    """Serialize a Member (+ latest membership) into the frontend MemberProfile."""
    return {
        "id": str(member.id),
        "email": member.email,
        "displayName": member.display_name,
        "avatarUrl": None,  # no avatar field yet
        "membershipPlan": membership.plan if membership else None,
        "membershipStatus": (
            _STATUS_MAP.get(membership.status, membership.status)
            if membership
            else None
        ),
        "isFoundingMember": member.founding_member_at is not None,
        "isAdmin": member.is_admin,
        "joinedAt": member.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


@router.get("/me")
async def get_me(
    member: Annotated[Member, Depends(get_current_member)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Return the authenticated member's profile."""
    membership = await _latest_membership(db, member.id)
    return _profile(member, membership)


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------


@router.patch("/me")
async def patch_me(
    body: MemberPatch,
    member: Annotated[Member, Depends(get_current_member)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Update the authenticated member's own profile fields."""
    if body.display_name is not None:
        member.display_name = body.display_name
        await db.commit()
    membership = await _latest_membership(db, member.id)
    return _profile(member, membership)


# ---------------------------------------------------------------------------
# GET /me/membership
# ---------------------------------------------------------------------------


@router.get("/me/membership")
async def get_membership(
    member: Annotated[Member, Depends(get_current_member)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Return the authenticated member's current membership / subscription status."""
    membership = await _latest_membership(db, member.id)
    return {
        "plan": membership.plan if membership else None,
        "status": (
            _STATUS_MAP.get(membership.status, membership.status)
            if membership
            else "none"
        ),
        "isFoundingMember": member.founding_member_at is not None,
        "currentPeriodEnd": (
            membership.current_period_end.isoformat()
            if membership and membership.current_period_end
            else None
        ),
    }
