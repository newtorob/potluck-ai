from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# POST /webhooks/stripe
# ---------------------------------------------------------------------------


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request) -> dict[str, str]:
    """Receive and process Stripe webhook events.

    The raw request body is read before any JSON parsing so that the
    Stripe signature can be verified against the unmodified bytes.

    Events handled (full implementation in Task 7 — billing_service.py):
    - checkout.session.completed
    - invoice.paid
    - invoice.payment_failed
    - customer.subscription.deleted

    The endpoint intentionally returns 200 for all well-formed requests so
    Stripe does not retry events that fail due to our internal errors. Error
    details are logged and sent to Sentry instead.
    """
    # Read raw body now — once consumed it cannot be re-read by a JSON parser.
    raw_body: bytes = await request.body()
    stripe_signature = request.headers.get("stripe-signature", "")

    # TODO (Task 7): call BillingService.handle_webhook(raw_body, stripe_signature)
    #                which does stripe.Webhook.construct_event() and dispatches
    #                to the appropriate handler.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Stripe webhook handler not yet implemented — see Task 7",
    )
