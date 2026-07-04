"""Admin roster + founding grant/revoke + billing summary, with auth gates."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from potluck_api.models.audit import AuditLog
from tests.factories import create_member, login


async def test_members_requires_auth(client):
    assert (await client.get("/admin/members")).status_code == 401


async def test_members_forbidden_for_non_admin(client, db):
    member = await create_member(db)
    headers = await login(db, member)
    assert (await client.get("/admin/members", headers=headers)).status_code == 403


async def test_members_lists_for_admin(client, db):
    admin = await create_member(db, is_admin=True)
    await create_member(db, email="a@example.com")
    await create_member(db, email="b@example.com")
    headers = await login(db, admin)
    r = await client.get("/admin/members", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 3
    assert len(data["members"]) == 3


async def test_grant_founding_bad_uuid_422(client, db):
    admin = await create_member(db, is_admin=True)
    headers = await login(db, admin)
    r = await client.post("/admin/members/not-a-uuid/grant-founding", headers=headers)
    assert r.status_code == 422


async def test_grant_founding_unknown_404(client, db):
    admin = await create_member(db, is_admin=True)
    headers = await login(db, admin)
    r = await client.post(f"/admin/members/{uuid.uuid4()}/grant-founding", headers=headers)
    assert r.status_code == 404


async def test_grant_founding_idempotent_and_audited(client, db):
    admin = await create_member(db, is_admin=True)
    target = await create_member(db, email="grant@example.com")
    admin_id, target_id = admin.id, target.id
    headers = await login(db, admin)

    r1 = await client.post(f"/admin/members/{target_id}/grant-founding", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["granted"] is True
    assert r1.json()["member"]["isFoundingMember"] is True

    r2 = await client.post(f"/admin/members/{target_id}/grant-founding", headers=headers)
    assert r2.json()["granted"] is False  # already founding, no-op

    # Exactly one audit row, actored by the admin.
    rows = (
        await db.scalars(
            select(AuditLog).where(AuditLog.action == "member.granted_founding")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].actor_id == admin_id
    assert rows[0].target_id == str(target_id)


async def test_revoke_founding_idempotent(client, db):
    admin = await create_member(db, is_admin=True)
    target = await create_member(db, founding=True)
    target_id = target.id
    headers = await login(db, admin)

    r1 = await client.delete(f"/admin/members/{target_id}/founding", headers=headers)
    assert r1.status_code == 200 and r1.json()["revoked"] is True

    r2 = await client.delete(f"/admin/members/{target_id}/founding", headers=headers)
    assert r2.json()["revoked"] is False


async def test_billing_summary_shape(client, db):
    admin = await create_member(db, is_admin=True, founding=True)
    headers = await login(db, admin)
    r = await client.get("/admin/billing", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["billingConfigured"] is False
    assert data["foundingMembers"] == 1
    assert data["revenueCents"] == 0
    assert data["recentEvents"] == []
