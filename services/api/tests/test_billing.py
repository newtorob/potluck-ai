"""Stripe billing: endpoint gates + the signed-webhook state machine.

Signs events with the stdlib HMAC (no Stripe account needed) and drives the
full lifecycle through the real /webhooks/stripe endpoint, asserting DB state.
"""
from __future__ import annotations

import time

from sqlalchemy import select

from potluck_api.models.billing import BillingEvent
from potluck_api.models.member import Member
from potluck_api.models.membership import Membership
from tests.factories import create_member, login, sign_stripe


async def _post_webhook(client, event):
    body, sig = sign_stripe(event)
    return await client.post(
        "/webhooks/stripe",
        content=body,
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )


def _checkout(member_id, *, plan, mode, customer, subscription=None, amount=2000):
    obj = {
        "id": "cs_" + plan,
        "mode": mode,
        "customer": customer,
        "client_reference_id": str(member_id),
        "metadata": {"member_id": str(member_id), "plan": plan},
        "amount_total": amount,
        "currency": "usd",
    }
    if subscription:
        obj["subscription"] = subscription
    return {"id": f"evt_co_{plan}", "type": "checkout.session.completed", "data": {"object": obj}}


# --------------------------------------------------------------------------- #
# Endpoint gates                                                              #
# --------------------------------------------------------------------------- #


async def test_checkout_requires_auth(client):
    assert (await client.post("/billing/checkout", json={"plan": "founding"})).status_code == 401


async def test_checkout_503_when_not_configured(client, db):
    headers = await login(db, await create_member(db))
    r = await client.post("/billing/checkout", json={"plan": "founding"}, headers=headers)
    assert r.status_code == 503


async def test_checkout_rejects_bad_plan(client, db):
    headers = await login(db, await create_member(db))
    r = await client.post("/billing/checkout", json={"plan": "bogus"}, headers=headers)
    assert r.status_code == 422


async def test_portal_503_when_not_configured(client, db):
    headers = await login(db, await create_member(db))
    assert (await client.post("/billing/portal", headers=headers)).status_code == 503


async def test_webhook_bad_signature_400(client):
    r = await client.post(
        "/webhooks/stripe",
        content=b'{"id":"evt_x","type":"invoice.paid","data":{"object":{}}}',
        headers={"stripe-signature": "t=1,v1=deadbeef", "content-type": "application/json"},
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Webhook state machine                                                       #
# --------------------------------------------------------------------------- #


async def test_founding_checkout_stamps_badge(client, db):
    member = await create_member(db, email="found@example.com")
    mid = member.id
    r = await _post_webhook(client, _checkout(mid, plan="founding", mode="payment", customer="cus_f"))
    assert r.status_code == 200

    db.expire_all()
    m = await db.get(Member, mid)
    assert m.founding_member_at is not None
    assert m.stripe_customer_id == "cus_f"

    ms = (await db.scalars(select(Membership).where(Membership.member_id == mid))).all()
    assert len(ms) == 1 and ms[0].plan == "founding" and ms[0].status == "active"

    be = await db.scalar(
        select(BillingEvent).where(BillingEvent.stripe_event_id == "evt_co_founding")
    )
    assert be is not None and be.amount_cents == 2000 and be.member_id == mid


async def test_duplicate_event_deduped(client, db):
    member = await create_member(db)
    mid = member.id
    event = _checkout(mid, plan="founding", mode="payment", customer="cus_d")
    event["id"] = "evt_dup"

    assert (await _post_webhook(client, event)).status_code == 200
    assert (await _post_webhook(client, event)).status_code == 200  # redelivery

    db.expire_all()
    ms = (await db.scalars(select(Membership).where(Membership.member_id == mid))).all()
    bes = (await db.scalars(select(BillingEvent).where(BillingEvent.stripe_event_id == "evt_dup"))).all()
    assert len(ms) == 1  # not doubled
    assert len(bes) == 1


async def test_subscription_lifecycle(client, db):
    member = await create_member(db, email="sub@example.com")
    mid = member.id

    await _post_webhook(
        client,
        _checkout(mid, plan="monthly", mode="subscription", customer="cus_s", subscription="sub_1", amount=1000),
    )
    db.expire_all()
    m = await db.get(Member, mid)
    assert m.founding_member_at is None  # a subscription is not a founding badge
    ms = await db.scalar(select(Membership).where(Membership.stripe_subscription_id == "sub_1"))
    assert ms.plan == "monthly" and ms.status == "active"

    period_end = int(time.time()) + 2_592_000
    await _post_webhook(client, {
        "id": "evt_inv_paid", "type": "invoice.paid",
        "data": {"object": {
            "id": "in_1", "subscription": "sub_1", "customer": "cus_s",
            "amount_paid": 1000, "currency": "usd",
            "lines": {"data": [{"period": {"end": period_end}}]},
        }},
    })
    db.expire_all()
    ms = await db.scalar(select(Membership).where(Membership.stripe_subscription_id == "sub_1"))
    assert ms.status == "active"
    assert ms.current_period_end is not None
    assert int(ms.current_period_end.timestamp()) == period_end

    await _post_webhook(client, {
        "id": "evt_inv_fail", "type": "invoice.payment_failed",
        "data": {"object": {"id": "in_2", "subscription": "sub_1", "customer": "cus_s", "amount_due": 1000, "currency": "usd"}},
    })
    db.expire_all()
    ms = await db.scalar(select(Membership).where(Membership.stripe_subscription_id == "sub_1"))
    assert ms.status == "past_due"

    await _post_webhook(client, {
        "id": "evt_sub_del", "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_s", "status": "canceled"}},
    })
    db.expire_all()
    ms = await db.scalar(select(Membership).where(Membership.stripe_subscription_id == "sub_1"))
    assert ms.status == "canceled"


async def test_unresolved_member_records_event_without_crash(client, db):
    # No client_reference_id / metadata / known customer -> event still recorded.
    r = await _post_webhook(client, {
        "id": "evt_orphan", "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_o", "mode": "payment", "customer": "cus_unknown", "amount_total": 500, "currency": "usd"}},
    })
    assert r.status_code == 200
    be = await db.scalar(select(BillingEvent).where(BillingEvent.stripe_event_id == "evt_orphan"))
    assert be is not None and be.member_id is None
