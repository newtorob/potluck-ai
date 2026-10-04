"""Browser-side device login: prefilled codes and one-click approval.

A browser that approved a terminal once can keep an HttpOnly web session, so
the next `potluck login` needs one click instead of an email round trip.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from potluck_api.config import get_settings
from potluck_api.models.device_grant import DeviceGrant
from potluck_api.models.session import Session
from potluck_api.services.email_service import EmailService

SITE = {"Origin": get_settings().app_url.rstrip("/")}
COOKIE = get_settings().session_cookie_name


async def email_proof(client, monkeypatch, email="cli@example.com"):
    messages = []

    async def capture(self, to, link):
        messages.append(link)

    monkeypatch.setattr(EmailService, "send_device_approval", capture)
    grant = (await client.post("/auth/device/start")).json()
    await client.post("/auth/device/request-link", json={"user_code": grant["user_code"], "email": email})
    return grant, messages[0].split("#token=")[1]


async def signed_in_browser(client, monkeypatch):
    """Approve one terminal by email with remember=True; the client keeps the cookie."""
    grant, proof = await email_proof(client, monkeypatch)
    body = {"token": proof, "user_code": grant["user_code"], "approved": True, "remember": True}
    response = await client.post("/auth/device/approve", json=body, headers=SITE)
    assert response.json() == {"status": "approved"}
    return grant


async def test_start_returns_a_complete_uri_and_keeps_the_plain_one_query_free(client):
    grant = (await client.post("/auth/device/start")).json()
    assert "?" not in grant["verification_uri"]  # released runtimes reject a query here
    assert grant["verification_uri_complete"] == grant["verification_uri"] + "?code=" + grant["user_code"]


async def test_a_new_browser_is_signed_out(client):
    response = await client.get("/auth/device/session")
    assert response.status_code == 200
    assert response.json() == {"signedIn": False}
    assert response.headers["cache-control"] == "no-store"


async def test_email_approval_without_remember_sets_no_cookie(client, db, monkeypatch):
    grant, proof = await email_proof(client, monkeypatch)
    body = {"token": proof, "user_code": grant["user_code"], "approved": True}
    response = await client.post("/auth/device/approve", json=body)
    assert response.json() == {"status": "approved"}
    assert COOKIE not in response.cookies
    assert (await client.get("/auth/device/session")).json() == {"signedIn": False}
    assert await db.scalar(select(func.count()).select_from(Session)) == 0


async def test_remembered_email_approval_signs_the_browser_in(client, db, monkeypatch):
    grant = await signed_in_browser(client, monkeypatch)
    assert (await client.get("/auth/device/session")).json() == {"signedIn": True, "email": "cli@example.com"}
    # The terminal still gets its own session; the browser holds a separate one.
    terminal = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
    assert terminal.status_code == 200
    assert await db.scalar(select(func.count()).select_from(Session)) == 2


async def test_declining_by_email_never_remembers_the_browser(client, monkeypatch):
    grant, proof = await email_proof(client, monkeypatch)
    body = {"token": proof, "user_code": grant["user_code"], "approved": False, "remember": True}
    response = await client.post("/auth/device/approve", json=body)
    assert response.json() == {"status": "denied"}
    assert (await client.get("/auth/device/session")).json() == {"signedIn": False}


async def test_signed_in_browser_approves_the_next_terminal_in_one_click(client, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    second = (await client.post("/auth/device/start")).json()
    response = await client.post("/auth/device/approve-session",
                                 json={"user_code": second["user_code"], "approved": True}, headers=SITE)
    assert response.status_code == 200
    assert response.json() == {"status": "approved"}
    terminal = (await client.post("/auth/device/token", json={"device_code": second["device_code"]})).json()
    assert terminal["email"] == "cli@example.com"
    me = await client.get("/me", headers={"Authorization": "Bearer " + terminal["token"]})
    assert me.json()["email"] == "cli@example.com"


async def test_one_click_accepts_a_lowercase_code_without_the_dash(client, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    second = (await client.post("/auth/device/start")).json()
    typed = second["user_code"].replace("-", "").lower()
    response = await client.post("/auth/device/approve-session", json={"user_code": typed, "approved": True}, headers=SITE)
    assert response.json() == {"status": "approved"}


async def test_one_click_decline_denies_the_terminal(client, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    second = (await client.post("/auth/device/start")).json()
    response = await client.post("/auth/device/approve-session",
                                 json={"user_code": second["user_code"], "approved": False}, headers=SITE)
    assert response.json() == {"status": "denied"}
    poll = await client.post("/auth/device/token", json={"device_code": second["device_code"]})
    assert poll.json()["error"] == "access_denied"


async def test_one_click_needs_a_web_session(client):
    grant = (await client.post("/auth/device/start")).json()
    response = await client.post("/auth/device/approve-session",
                                 json={"user_code": grant["user_code"], "approved": True}, headers=SITE)
    assert response.status_code == 401
    assert response.json() == {"error": "not_signed_in"}


async def test_one_click_ignores_bearer_tokens(client, monkeypatch):
    """Only the browser's cookie counts; a terminal token cannot approve other terminals."""
    first = await signed_in_browser(client, monkeypatch)
    token = (await client.post("/auth/device/token", json={"device_code": first["device_code"]})).json()["token"]
    client.cookies.clear()
    second = (await client.post("/auth/device/start")).json()
    response = await client.post("/auth/device/approve-session", json={"user_code": second["user_code"], "approved": True},
                                 headers={**SITE, "Authorization": "Bearer " + token})
    assert response.status_code == 401
    assert (await client.get("/auth/device/session", headers={"Authorization": "Bearer " + token})).json() == {"signedIn": False}


async def test_one_click_rejects_other_origins(client, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    second = (await client.post("/auth/device/start")).json()
    for headers in ({"Origin": "https://evil.example"}, {}):
        response = await client.post("/auth/device/approve-session",
                                     json={"user_code": second["user_code"], "approved": True}, headers=headers)
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden_origin"}
    poll = await client.post("/auth/device/token", json={"device_code": second["device_code"]})
    assert poll.json()["error"] == "authorization_pending"


async def test_one_click_rejects_unknown_expired_and_used_codes(client, db, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    unknown = await client.post("/auth/device/approve-session", json={"user_code": "BCDF-GHJK", "approved": True}, headers=SITE)
    assert (unknown.status_code, unknown.json()) == (400, {"error": "invalid_user_code"})

    second = (await client.post("/auth/device/start")).json()
    body = {"user_code": second["user_code"], "approved": True}
    assert (await client.post("/auth/device/approve-session", json=body, headers=SITE)).json() == {"status": "approved"}
    again = await client.post("/auth/device/approve-session", json=body, headers=SITE)
    assert (again.status_code, again.json()) == (400, {"error": "already_used"})

    third = (await client.post("/auth/device/start")).json()
    await db.execute(update(DeviceGrant).where(DeviceGrant.status == "pending")
                     .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db.commit()
    late = await client.post("/auth/device/approve-session", json={"user_code": third["user_code"], "approved": True}, headers=SITE)
    assert (late.status_code, late.json()) == (400, {"error": "expired_token"})


async def test_signing_out_ends_the_web_session(client, monkeypatch):
    await signed_in_browser(client, monkeypatch)
    assert (await client.post("/auth/logout")).status_code == 204
    assert (await client.get("/auth/device/session")).json() == {"signedIn": False}
