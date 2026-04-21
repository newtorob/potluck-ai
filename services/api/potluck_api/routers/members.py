from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from potluck_api.middleware.auth import get_current_member

router = APIRouter(tags=["members"])


class MemberPatch(BaseModel):
    """Fields the member is allowed to update on their own profile."""

    display_name: str | None = None


# ---------------------------------------------------------------------------
# GET /me
# ---------------------------------------------------------------------------


@router.get("/me")
async def get_me(
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, object]:
    """Return the authenticated member's profile.

    Full implementation in Task 6.
    """
    # TODO (Task 6): return MemberResponse.model_validate(member)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="GET /me not yet implemented — see Task 6",
    )


# ---------------------------------------------------------------------------
# PATCH /me
# ---------------------------------------------------------------------------


@router.patch("/me")
async def patch_me(
    body: MemberPatch,
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, object]:
    """Update the authenticated member's profile fields.

    Full implementation in Task 6.
    """
    # TODO (Task 6): call MemberService.update(member, body)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="PATCH /me not yet implemented — see Task 6",
    )


# ---------------------------------------------------------------------------
# GET /me/membership
# ---------------------------------------------------------------------------


@router.get("/me/membership")
async def get_membership(
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, object]:
    """Return the authenticated member's current membership / subscription status.

    Full implementation in Task 6.
    """
    # TODO (Task 6): call MemberService.get_membership(member.id)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="GET /me/membership not yet implemented — see Task 6",
    )
