"""Magic-link auth: request-link, callback/session, /me gate, logout revocation."""
from __future__ import annotations

from potluck_api.services.auth_service import AuthService
from tests.factories import create_member, login


async def test_request_link_returns_202(client):
    r = await client.post("/auth/request-link", json={"email": "new@example.com"})
    assert r.status_code == 202
    assert r.json()["status"] == "sent"


async def test_request_link_rejects_bad_email(client):
    r = await client.post("/auth/request-link", json={"email": "not-an-email"})
    assert r.status_code == 422


async def test_me_requires_auth(client):
    assert (await client.get("/me")).status_code == 401


async def test_callback_issues_session_then_me_works(client, db):
    token = AuthService(db).create_magic_link("founder@example.com").split("token=", 1)[1]
    r = await client.get(f"/auth/callback?token={token}")
    assert r.status_code == 200
    assert r.json()["memberId"]
    # The session cookie is now in the client jar, so /me works with no header.
    me = await client.get("/me")
    assert me.status_code == 200
    assert me.json()["email"] == "founder@example.com"


async def test_callback_rejects_garbage_token(client):
    assert (await client.get("/auth/callback?token=not.a.jwt")).status_code == 400


async def test_logout_revokes_session(client, db):
    token = AuthService(db).create_magic_link("bye@example.com").split("token=", 1)[1]
    await client.get(f"/auth/callback?token={token}")
    assert (await client.get("/me")).status_code == 200
    assert (await client.post("/auth/logout")).status_code == 204
    # Server-side revocation: the (now-cleared) session no longer authenticates.
    assert (await client.get("/me")).status_code == 401


async def test_bearer_fallback_authenticates(client, db):
    member = await create_member(db, email="bearer@example.com")
    headers = await login(db, member)
    me = await client.get("/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "bearer@example.com"
