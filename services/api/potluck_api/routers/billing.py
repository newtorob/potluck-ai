from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db
from potluck_api.middleware.auth import get_current_member
from potluck_api.models.member import Member
from potluck_api.services.billing_service import (
    BillingError,
    BillingNotConfigured,
    BillingService,
)

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
    member: Annotated[Member, Depends(get_current_member)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Create a Stripe Checkout session and return the redirect URL.

    - ``founding``: one-time founding member payment (STRIPE_FOUNDING_PRICE_ID)
    - ``monthly``:  recurring monthly dues (STRIPE_DUES_PRICE_ID)
    """
    try:
        url = await BillingService(db).create_checkout(member, body.plan)
    except BillingNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except BillingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"url": url}


# ---------------------------------------------------------------------------
# POST /billing/portal
# ---------------------------------------------------------------------------


@router.post("/portal")
async def billing_portal(
    member: Annotated[Member, Depends(get_current_member)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Create a Stripe Billing Portal session and return the redirect URL."""
    try:
        url = await BillingService(db).create_portal_session(member)
    except BillingNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except BillingError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"url": url}
