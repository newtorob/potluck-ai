from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db
from potluck_api.services.billing_service import (
    BillingService,
    BillingSignatureError,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# POST /webhooks/stripe
# ---------------------------------------------------------------------------


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Receive and process Stripe webhook events.

    The raw request body is read before any JSON parsing so the Stripe signature
    can be verified against the unmodified bytes.

    Events handled:
    - checkout.session.completed
    - invoice.paid
    - invoice.payment_failed
    - customer.subscription.deleted

    Response contract (aligned with Stripe retry semantics):
    - 400 on a bad/missing signature — a forged or misconfigured event; retrying
      it would never succeed, so we tell Stripe not to.
    - 200 once the event is processed (or safely deduped / ignored).
    - 500 on an unexpected internal error — processing is atomic and events are
      idempotent (deduped by id), so letting Stripe retry is the durable choice
      rather than swallowing the failure.
    """
    # Read raw body now — once consumed it cannot be re-read by a JSON parser.
    raw_body: bytes = await request.body()
    stripe_signature = request.headers.get("stripe-signature", "")

    try:
        await BillingService(db).handle_webhook(raw_body, stripe_signature)
    except BillingSignatureError as exc:
        logger.warning("stripe_webhook_bad_signature", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return {"status": "ok"}
