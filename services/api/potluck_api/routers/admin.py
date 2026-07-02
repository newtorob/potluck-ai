from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db
from potluck_api.middleware.auth import require_admin
from potluck_api.models.audit import AuditLog
from potluck_api.models.member import Member
from potluck_api.models.membership import Membership
from potluck_api.routers.members import _STATUS_MAP

router = APIRouter(tags=["admin"])

# All routes in this module require the require_admin dependency.
# require_admin calls get_current_member first (401 if not authed),
# then asserts is_admin == True (403 if not admin).


def _member_summary(member: Member, membership: Membership | None) -> dict[str, object]:
    """Serialize a Member (+ latest membership) for the admin view.

    Superset of the self-serve MemberProfile: also exposes admin-only fields
    (isAdmin, foundingMemberAt, stripeCustomerId) an operator needs.
    """
    return {
        "id": str(member.id),
        "email": member.email,
        "displayName": member.display_name,
        "isAdmin": member.is_admin,
        "isFoundingMember": member.founding_member_at is not None,
        "foundingMemberAt": (
            member.founding_member_at.isoformat()
            if member.founding_member_at
            else None
        ),
        "membershipPlan": membership.plan if membership else None,
        "membershipStatus": (
            _STATUS_MAP.get(membership.status, membership.status)
            if membership
            else None
        ),
        "stripeCustomerId": member.stripe_customer_id,
        "joinedAt": member.created_at.isoformat(),
    }


async def _latest_memberships_for(
    db: AsyncSession, member_ids: list[object]
) -> dict[object, Membership]:
    """Return {member_id: latest Membership} for the given members in one query.

    Rows come back newest-first, so the first one seen per member_id is the
    latest; setdefault keeps it and ignores older rows.
    """
    if not member_ids:
        return {}
    rows = (
        await db.scalars(
            select(Membership)
            .where(Membership.member_id.in_(member_ids))
            .order_by(Membership.created_at.desc())
        )
    ).all()
    latest: dict[object, Membership] = {}
    for ms in rows:
        latest.setdefault(ms.member_id, ms)
    return latest


async def _load_member(db: AsyncSession, member_id: object) -> Member:
    """Fetch a member by id or raise 404."""
    member = await db.get(Member, member_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found",
        )
    return member


# ---------------------------------------------------------------------------
# GET /admin/members
# ---------------------------------------------------------------------------


@router.get("/members")
async def admin_list_members(
    _: Annotated[Member, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """List members with their latest membership (admin only).

    Paginated (newest members first) so the response stays bounded as the
    roster grows. ``total`` is the full member count for the pager.
    """
    total = await db.scalar(select(func.count()).select_from(Member)) or 0
    members = (
        await db.scalars(
            select(Member)
            .order_by(Member.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    latest = await _latest_memberships_for(db, [m.id for m in members])
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "members": [_member_summary(m, latest.get(m.id)) for m in members],
    }


# ---------------------------------------------------------------------------
# POST /admin/members/{member_id}/grant-founding
# ---------------------------------------------------------------------------


@router.post("/members/{member_id}/grant-founding")
async def admin_grant_founding(
    member_id: uuid.UUID,
    admin: Annotated[Member, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Grant founding-member status to a member (admin only).

    Idempotent: if the member is already a founding member the original grant
    timestamp is preserved and no duplicate audit row is written. ``granted``
    reports whether this call changed anything.
    """
    member = await _load_member(db, member_id)

    granted = False
    if member.founding_member_at is None:
        member.founding_member_at = datetime.now(timezone.utc)
        db.add(
            AuditLog(
                actor_id=admin.id,
                action="member.granted_founding",
                target_type="member",
                target_id=str(member.id),
                extra={"email": member.email},
            )
        )
        await db.commit()
        await db.refresh(member)
        granted = True

    membership = (await _latest_memberships_for(db, [member.id])).get(member.id)
    return {"granted": granted, "member": _member_summary(member, membership)}


# ---------------------------------------------------------------------------
# DELETE /admin/members/{member_id}/founding
# ---------------------------------------------------------------------------


@router.delete("/members/{member_id}/founding")
async def admin_revoke_founding(
    member_id: uuid.UUID,
    admin: Annotated[Member, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Revoke founding-member status (admin only).

    Idempotent: revoking a member who is not founding is a no-op. ``revoked``
    reports whether this call changed anything.
    """
    member = await _load_member(db, member_id)

    revoked = False
    if member.founding_member_at is not None:
        member.founding_member_at = None
        db.add(
            AuditLog(
                actor_id=admin.id,
                action="member.revoked_founding",
                target_type="member",
                target_id=str(member.id),
                extra={"email": member.email},
            )
        )
        await db.commit()
        await db.refresh(member)
        revoked = True

    membership = (await _latest_memberships_for(db, [member.id])).get(member.id)
    return {"revoked": revoked, "member": _member_summary(member, membership)}


# ---------------------------------------------------------------------------
# GET /admin/billing
# ---------------------------------------------------------------------------


@router.get("/billing")
async def admin_billing_summary(
    _: Annotated[Member, Depends(require_admin)],
) -> dict[str, object]:
    """Billing overview: active subscriptions, MRR, etc. (admin only).

    Deferred to Task 7 (needs the Stripe integration for real revenue data).
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Admin billing summary not yet implemented",
    )
