from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from potluck_api.middleware.auth import require_admin

router = APIRouter(tags=["admin"])

# All routes in this module require the require_admin dependency.
# require_admin calls get_current_member first (401 if not authed),
# then asserts is_admin == True (403 if not admin).


@router.get("/members")
async def admin_list_members(
    _: Annotated[object, Depends(require_admin)],
) -> list[object]:
    """List all members (admin only).

    Full implementation in Task 6/7.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Admin member list not yet implemented",
    )


@router.get("/billing")
async def admin_billing_summary(
    _: Annotated[object, Depends(require_admin)],
) -> dict[str, object]:
    """Billing overview — active subscriptions, MRR, etc. (admin only).

    Full implementation in Task 7.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Admin billing summary not yet implemented",
    )
