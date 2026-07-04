"""Member profile: /me, PATCH /me, /me/membership."""
from __future__ import annotations

from tests.factories import create_member, login


async def test_me_returns_profile(client, db):
    member = await create_member(db, email="prof@example.com")
    headers = await login(db, member)
    r = await client.get("/me", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["email"] == "prof@example.com"
    assert data["isAdmin"] is False
    assert data["isFoundingMember"] is False
    assert data["membershipPlan"] is None


async def test_patch_me_updates_display_name(client, db):
    member = await create_member(db)
    headers = await login(db, member)
    r = await client.patch("/me", json={"display_name": "Ada"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["displayName"] == "Ada"
    # Persisted across requests.
    assert (await client.get("/me", headers=headers)).json()["displayName"] == "Ada"


async def test_membership_none_when_no_plan(client, db):
    member = await create_member(db)
    headers = await login(db, member)
    r = await client.get("/me/membership", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["plan"] is None
    assert data["status"] == "none"
    assert data["isFoundingMember"] is False


async def test_founding_flag_reflected(client, db):
    member = await create_member(db, founding=True)
    headers = await login(db, member)
    assert (await client.get("/me", headers=headers)).json()["isFoundingMember"] is True


async def test_admin_flag_reflected(client, db):
    member = await create_member(db, is_admin=True)
    headers = await login(db, member)
    assert (await client.get("/me", headers=headers)).json()["isAdmin"] is True
