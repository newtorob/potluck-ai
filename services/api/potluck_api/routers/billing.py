from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from potluck_api.middleware.auth import get_current_member

router = APIRouter(tags=["billing"])


class CheckoutRequest(BaseModel):
    """Request body for POST /billing/checkout."""

    plan: Literal["founding", "monthly"]


# ---------------------------------------------------------------------------
# POST /billing/checkout
# ---------------------------------------------------------------------------


@router.post("/checkout")
async def create_checkout(
    body: CheckoutRequest,
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, str]:
    """Create a Stripe Checkout session and return the redirect URL.

    - ``founding``: one-time founding member payment (STRIPE_FOUNDING_PRICE_ID)
    - ``monthly``:  recurring monthly dues (STRIPE_DUES_PRICE_ID)

    Full implementation in Task 7 (billing_service.py).
    """
    # TODO (Task 7): call BillingService.create_checkout(member, body.plan)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Checkout not yet implemented — see Task 7",
    )


# ---------------------------------------------------------------------------
# POST /billing/portal
# ---------------------------------------------------------------------------


@router.post("/portal")
async def billing_portal(
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, str]:
    """Create a Stripe Billing Portal session and return the redirect URL.

    Full implementation in Task 7.
    """
    # TODO (Task 7): call BillingService.create_portal_session(member)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Billing portal not yet implemented — see Task 7",
    )
