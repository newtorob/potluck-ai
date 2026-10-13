"""Stripe integration: Checkout sessions, Billing Portal, and webhook dispatch.

Full implementation spec: guide Section 5.6.
Task: 7

Responsibilities:
- create_checkout(member, plan) -> str - returns Stripe Checkout URL
- create_portal_session(member) -> str - returns Stripe Billing Portal URL
- handle_webhook(raw_body, signature) -> None - verifies Stripe signature,
  dispatches to handlers for:
    - checkout.session.completed    -> create Membership, stamp founding badge
    - invoice.paid                  -> extend current_period_end, mark active
    - invoice.payment_failed        -> mark membership past_due
    - customer.subscription.deleted -> mark membership canceled

Uses STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_FOUNDING_PRICE_ID,
STRIPE_DUES_PRICE_ID from settings.

Configuration gate (mirrors EmailService): when no real STRIPE_SECRET_KEY is set
(local dev / no account yet) the checkout + portal calls raise
BillingNotConfigured so the frontend can show "billing unavailable" rather than
crashing. The webhook path only needs STRIPE_WEBHOOK_SECRET to verify signatures,
so it can be exercised in dev without a live secret key.

Idempotency + durability: every processed event is recorded in billing_events,
keyed by the Stripe event id. handle_webhook dedupes on that id, and does the
event insert plus all state mutations in a SINGLE transaction. If any handler
raises, the whole transaction rolls back (no billing_events row is written) and
Stripe's automatic retry re-delivers the event for a clean re-process.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.models.audit import AuditLog
from potluck_api.models.billing import BillingEvent
from potluck_api.models.member import Member
from potluck_api.models.membership import Membership

logger = structlog.get_logger(__name__)

# Plans that map to a one-time payment vs a recurring subscription.
_FOUNDING = "founding"
_MONTHLY = "monthly"


class BillingError(Exception):
    """Base class for billing failures the caller maps to an HTTP status."""


class BillingNotConfigured(BillingError):
    """Stripe keys/prices are not set - the operation cannot proceed (503)."""


class BillingSignatureError(BillingError):
    """A webhook payload failed Stripe signature verification (400)."""


def _is_real_key(key: str) -> bool:
    """True only for a usable Stripe key.

    A dev environment may leave the key empty or set a placeholder; treat both
    as "not configured" so we never call Stripe with a bogus credential.
    """
    key = key.strip()
    return bool(key) and "placeholder" not in key.lower()


def _epoch_to_dt(ts: object) -> datetime | None:
    """Convert a Stripe unix timestamp (seconds) to an aware UTC datetime."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class BillingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        """True when a real Stripe secret key is configured."""
        return _is_real_key(self.settings.stripe_secret_key)

    @property
    def _app_base(self) -> str:
        return self.settings.app_url.rstrip("/")

    # -- Checkout ----------------------------------------------------------

    async def create_checkout(self, member: Member, plan: str) -> str:
        """Create a Stripe Checkout Session and return its redirect URL.

        - ``founding`` -> one-time payment (STRIPE_FOUNDING_PRICE_ID)
        - ``monthly``  -> recurring subscription (STRIPE_DUES_PRICE_ID)
        """
        if not self.enabled:
            raise BillingNotConfigured("Billing is not configured")

        if plan == _FOUNDING:
            price = self.settings.stripe_founding_price_id
            mode = "payment"
        else:
            price = self.settings.stripe_dues_price_id
            mode = "subscription"
        if not price:
            raise BillingNotConfigured(f"No Stripe price configured for plan {plan!r}")

        import stripe  # lazy - only needed when actually talking to Stripe

        stripe.api_key = self.settings.stripe_secret_key
        customer_id = await self._ensure_customer(member, stripe)

        def _create() -> object:
            return stripe.checkout.Session.create(
                mode=mode,
                customer=customer_id,
                line_items=[{"price": price, "quantity": 1}],
                client_reference_id=str(member.id),
                metadata={"member_id": str(member.id), "plan": plan},
                success_url=f"{self._app_base}/membership?billing=success",
                cancel_url=f"{self._app_base}/membership?billing=cancel",
            )

        session = await asyncio.to_thread(_create)
        logger.info("stripe_checkout_created", member_id=str(member.id), plan=plan)
        return session.url

    async def create_portal_session(self, member: Member) -> str:
        """Create a Stripe Billing Portal session and return its redirect URL."""
        if not self.enabled:
            raise BillingNotConfigured("Billing is not configured")
        if not member.stripe_customer_id:
            raise BillingError("No billing account exists for this member yet")

        import stripe  # lazy

        stripe.api_key = self.settings.stripe_secret_key

        def _create() -> object:
            return stripe.billing_portal.Session.create(
                customer=member.stripe_customer_id,
                return_url=f"{self._app_base}/membership",
            )

        session = await asyncio.to_thread(_create)
        logger.info("stripe_portal_created", member_id=str(member.id))
        return session.url

    async def _ensure_customer(self, member: Member, stripe: object) -> str:
        """Return the member's Stripe customer id, creating one if needed."""
        if member.stripe_customer_id:
            return member.stripe_customer_id

        def _create() -> object:
            return stripe.Customer.create(  # type: ignore[attr-defined]
                email=member.email,
                metadata={"member_id": str(member.id)},
            )

        customer = await asyncio.to_thread(_create)
        member.stripe_customer_id = customer.id
        await self.db.commit()
        logger.info(
            "stripe_customer_created",
            member_id=str(member.id),
            customer_id=customer.id,
        )
        return customer.id

    # -- Webhook -----------------------------------------------------------

    def _verify(self, raw_body: bytes, signature: str) -> object:
        """Verify the Stripe signature and return the parsed event.

        Raises BillingSignatureError on a missing secret, malformed payload, or
        a signature mismatch - all of which the endpoint maps to HTTP 400.
        """
        secret = self.settings.stripe_webhook_secret
        if not secret:
            raise BillingSignatureError("Webhook secret not configured")

        import stripe  # lazy

        try:
            return stripe.Webhook.construct_event(raw_body, signature, secret)
        except stripe.error.SignatureVerificationError as exc:  # type: ignore[attr-defined]
            raise BillingSignatureError("Invalid webhook signature") from exc
        except ValueError as exc:
            raise BillingSignatureError("Invalid webhook payload") from exc

    async def handle_webhook(self, raw_body: bytes, signature: str) -> None:
        """Verify, dedupe, record, and dispatch a single Stripe webhook event.

        All work happens in one transaction so a failed dispatch rolls back and
        Stripe's retry re-processes the event cleanly (see module docstring).
        """
        event = self._verify(raw_body, signature)
        event_id = event["id"]
        event_type = event["type"]

        # Idempotency: skip events we have already recorded.
        existing = await self.db.scalar(
            select(BillingEvent).where(BillingEvent.stripe_event_id == event_id)
        )
        if existing is not None:
            logger.info("stripe_webhook_duplicate", event_id=event_id, type=event_type)
            return

        obj = event["data"]["object"]
        member = await self._resolve_member(obj)
        amount_cents, currency = self._amount(event_type, obj)

        self.db.add(
            BillingEvent(
                member_id=member.id if member else None,
                stripe_event_id=event_id,
                event_type=event_type,
                amount_cents=amount_cents,
                currency=currency,
                payload=self._payload_dict(raw_body),
            )
        )

        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "invoice.paid": self._on_invoice_paid,
            "invoice.payment_failed": self._on_payment_failed,
            "customer.subscription.deleted": self._on_subscription_deleted,
        }.get(event_type)

        if handler is not None:
            await handler(obj, member)
        else:
            logger.info("stripe_webhook_ignored", event_id=event_id, type=event_type)

        await self.db.commit()
        logger.info("stripe_webhook_processed", event_id=event_id, type=event_type)

    # -- Event handlers ----------------------------------------------------

    async def _on_checkout_completed(
        self, obj: object, member: Member | None
    ) -> None:
        """A Checkout session finished: create the membership, stamp founding."""
        if member is None:
            logger.warning("stripe_checkout_unresolved_member")
            return

        customer_id = obj.get("customer")  # type: ignore[attr-defined]
        if customer_id and not member.stripe_customer_id:
            member.stripe_customer_id = customer_id

        metadata = obj.get("metadata") or {}  # type: ignore[attr-defined]
        mode = obj.get("mode")  # type: ignore[attr-defined]
        plan = metadata.get("plan") or (_FOUNDING if mode == "payment" else _MONTHLY)

        if plan == _FOUNDING:
            # Paying the founding fee IS what confers the founding badge. This is
            # distinct from the admin bootstrap, which grants is_admin only.
            if member.founding_member_at is None:
                member.founding_member_at = datetime.now(timezone.utc)
            self.db.add(
                Membership(member_id=member.id, plan=_FOUNDING, status="active")
            )
        else:
            self.db.add(
                Membership(
                    member_id=member.id,
                    plan=_MONTHLY,
                    status="active",
                    stripe_subscription_id=obj.get("subscription"),  # type: ignore[attr-defined]
                )
            )

        self._audit(
            member,
            "billing.checkout_completed",
            {"plan": plan, "amountCents": obj.get("amount_total")},  # type: ignore[attr-defined]
        )

    async def _on_invoice_paid(self, obj: object, member: Member | None) -> None:
        """A subscription invoice was paid: mark active + extend the period."""
        membership = await self._membership_for(obj, member)
        if membership is None:
            logger.warning("stripe_invoice_paid_no_membership")
            return
        membership.status = "active"
        period_end = self._invoice_period_end(obj)
        if period_end is not None:
            membership.current_period_end = period_end
        if member is not None:
            self._audit(member, "billing.invoice_paid", {})

    async def _on_payment_failed(self, obj: object, member: Member | None) -> None:
        """A subscription invoice failed: mark the membership past_due."""
        membership = await self._membership_for(obj, member)
        if membership is None:
            return
        membership.status = "past_due"
        if member is not None:
            self._audit(member, "billing.payment_failed", {})

    async def _on_subscription_deleted(
        self, obj: object, member: Member | None
    ) -> None:
        """A subscription was canceled: mark the membership canceled."""
        sub_id = obj.get("id")  # type: ignore[attr-defined]
        membership = None
        if sub_id:
            membership = await self.db.scalar(
                select(Membership).where(Membership.stripe_subscription_id == sub_id)
            )
        if membership is None:
            return
        membership.status = "canceled"
        if member is not None:
            self._audit(member, "billing.subscription_deleted", {})

    # -- Helpers -----------------------------------------------------------

    def _audit(self, member: Member, action: str, extra: dict) -> None:
        """Append a system-actor (actor_id=None) audit row for a billing event."""
        self.db.add(
            AuditLog(
                actor_id=None,
                action=action,
                target_type="member",
                target_id=str(member.id),
                extra={"email": member.email, **extra},
            )
        )

    async def _resolve_member(self, obj: object) -> Member | None:
        """Find the Member behind a webhook object via metadata then customer."""
        metadata = obj.get("metadata") or {}  # type: ignore[attr-defined]
        raw_id = obj.get("client_reference_id") or metadata.get("member_id")  # type: ignore[attr-defined]
        if raw_id:
            try:
                member = await self.db.get(Member, uuid.UUID(str(raw_id)))
            except ValueError:
                member = None
            if member is not None:
                return member

        customer_id = obj.get("customer")  # type: ignore[attr-defined]
        if customer_id:
            return await self.db.scalar(
                select(Member).where(Member.stripe_customer_id == customer_id)
            )
        return None

    async def _membership_for(
        self, obj: object, member: Member | None
    ) -> Membership | None:
        """Resolve the Membership an invoice event applies to."""
        sub_id = obj.get("subscription")  # type: ignore[attr-defined]
        if sub_id:
            membership = await self.db.scalar(
                select(Membership).where(Membership.stripe_subscription_id == sub_id)
            )
            if membership is not None:
                return membership
        if member is not None:
            return await self.db.scalar(
                select(Membership)
                .where(Membership.member_id == member.id)
                .order_by(Membership.created_at.desc())
            )
        return None

    @staticmethod
    def _amount(event_type: str, obj: object) -> tuple[int | None, str | None]:
        """Extract (amount_cents, currency) for the events that carry money."""
        if event_type == "checkout.session.completed":
            return obj.get("amount_total"), obj.get("currency")  # type: ignore[attr-defined]
        if event_type in ("invoice.paid", "invoice.payment_failed"):
            amount = obj.get("amount_paid")  # type: ignore[attr-defined]
            if amount is None:
                amount = obj.get("amount_due")  # type: ignore[attr-defined]
            return amount, obj.get("currency")  # type: ignore[attr-defined]
        return None, None

    @staticmethod
    def _invoice_period_end(obj: object) -> datetime | None:
        """Best-effort subscription period end from an invoice object."""
        lines = obj.get("lines") or {}  # type: ignore[attr-defined]
        data = lines.get("data") or []
        if data:
            period = data[0].get("period") or {}
            end = period.get("end")
            if end:
                return _epoch_to_dt(end)
        return _epoch_to_dt(obj.get("period_end"))  # type: ignore[attr-defined]

    @staticmethod
    def _payload_dict(raw_body: bytes) -> dict | None:
        """Parse the raw webhook body to store on the BillingEvent row."""
        try:
            parsed = json.loads(raw_body)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
