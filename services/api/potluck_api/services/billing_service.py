"""Stripe integration: Checkout sessions, Billing Portal, and webhook dispatch.

Full implementation spec: guide Section 5.6.
Task: 7

Responsibilities:
- create_checkout(member, plan) -> str  — returns Stripe Checkout URL
- create_portal_session(member) -> str  — returns Stripe Billing Portal URL
- handle_webhook(raw_body, signature) -> None  — verifies Stripe signature,
  dispatches to handlers for:
    - checkout.session.completed   -> create Membership row, mark founding badge
    - invoice.paid                 -> extend current_period_end
    - invoice.payment_failed       -> notify member
    - customer.subscription.deleted -> deactivate Membership

Uses STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_FOUNDING_PRICE_ID,
STRIPE_DUES_PRICE_ID from settings.
"""
from __future__ import annotations


class BillingService:
    pass
