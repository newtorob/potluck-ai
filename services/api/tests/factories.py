"""Test helpers: seed members/sessions and sign Stripe webhook payloads."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.models.member import Member
from potluck_api.models.session import Session


async def create_member(
    db: AsyncSession,
    *,
    email: str | None = None,
    is_admin: bool = False,
    founding: bool = False,
    stripe_customer_id: str | None = None,
) -> Member:
    """Insert and return a Member."""
    member = Member(
        email=email or f"m-{uuid.uuid4().hex[:10]}@example.com",
        is_admin=is_admin,
        stripe_customer_id=stripe_customer_id,
    )
    if founding:
        member.founding_member_at = datetime.now(timezone.utc)
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def login(db: AsyncSession, member: Member) -> dict[str, str]:
    """Mint a session for a member and return an Authorization: Bearer header."""
    raw = secrets.token_urlsafe(48)
    db.add(
        Session(
            member_id=member.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


def sign_stripe(event: dict) -> tuple[bytes, str]:
    """Return (raw_body, stripe-signature header) for a webhook event.

    Reproduces Stripe's scheme: HMAC-SHA256 over ``{timestamp}.{payload}`` with
    the configured webhook secret, so BillingService._verify accepts it.
    """
    secret = get_settings().stripe_webhook_secret
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload.encode(), f"t={ts},v1={sig}"
