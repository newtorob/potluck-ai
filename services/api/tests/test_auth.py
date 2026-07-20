"""Magic-link auth: request-link, callback/session, /me gate, logout revocation."""
from __future__ import annotations

from sqlalchemy import func, select

from potluck_api.config import get_settings
from potluck_api.middleware.rate_limit import limiter
from potluck_api.models.member import Member
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


# ---------------------------------------------------------------------------
# FIX 1 — magic links are single-use
# ---------------------------------------------------------------------------


async def test_magic_link_rejects_second_redemption(client, db):
    token = (
        AuthService(db).create_magic_link("onceonly@example.com").split("token=", 1)[1]
    )
    r1 = await client.get(f"/auth/callback?token={token}")
    assert r1.status_code == 200

    r2 = await client.get(f"/auth/callback?token={token}")
    assert r2.status_code == 400

    # The first redemption's session is untouched — replay didn't revoke it.
    assert r1.json()["memberId"]


# ---------------------------------------------------------------------------
# FIX 2 — production cookie is SameSite=Lax (not None)
# ---------------------------------------------------------------------------


async def test_callback_prod_cookie_is_samesite_lax(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "env", "production")

    token = (
        AuthService(db).create_magic_link("prodcookie@example.com").split("token=", 1)[1]
    )
    r = await client.get(f"/auth/callback?token={token}")
    assert r.status_code == 200

    cookie_header = r.headers.get("set-cookie", "").lower()
    assert "samesite=lax" in cookie_header
    assert "secure" in cookie_header


# ---------------------------------------------------------------------------
# FIX 3 — ADMIN_EMAILS is re-evaluated every sign-in; email lookup is
# case-insensitive
# ---------------------------------------------------------------------------


async def test_admin_allowlist_promotes_and_demotes_on_sign_in(client, db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_emails", "council@example.com")

    # First sign-in (mixed case) — creates the member and grants admin from
    # the allowlist.
    token1 = (
        AuthService(db).create_magic_link("Council@Example.com").split("token=", 1)[1]
    )
    r1 = await client.get(f"/auth/callback?token={token1}")
    assert r1.status_code == 200
    assert r1.json()["email"] == "council@example.com"  # normalized on mint

    member = await db.scalar(
        select(Member).where(func.lower(Member.email) == "council@example.com")
    )
    assert member.is_admin is True

    # Remove the address from the allowlist, then sign in again with a
    # different case variant — must resolve to the SAME member row and
    # demote it, not create a second one.
    monkeypatch.setattr(settings, "admin_emails", "")
    token2 = (
        AuthService(db).create_magic_link("COUNCIL@EXAMPLE.COM").split("token=", 1)[1]
    )
    r2 = await client.get(f"/auth/callback?token={token2}")
    assert r2.status_code == 200
    assert r2.json()["memberId"] == str(member.id)

    total = await db.scalar(
        select(func.count())
        .select_from(Member)
        .where(func.lower(Member.email) == "council@example.com")
    )
    assert total == 1

    await db.refresh(member)
    assert member.is_admin is False


# ---------------------------------------------------------------------------
# FIX 4 — /auth/request-link caps per-recipient independent of the per-IP cap
# ---------------------------------------------------------------------------


async def test_request_link_caps_per_recipient(client, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)
    email = "victim@example.com"

    for _ in range(3):
        r = await client.post("/auth/request-link", json={"email": email})
        assert r.status_code == 202

    r4 = await client.post("/auth/request-link", json={"email": email})
    assert r4.status_code == 429


# ---------------------------------------------------------------------------
# FIX 5 — /auth/callback returns the signed-in member's email
# ---------------------------------------------------------------------------


async def test_callback_returns_email(client, db):
    token = (
        AuthService(db).create_magic_link("returns-email@example.com").split("token=", 1)[1]
    )
    r = await client.get(f"/auth/callback?token={token}")
    assert r.status_code == 200
    assert r.json()["email"] == "returns-email@example.com"
