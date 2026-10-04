import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from potluck_api.models.device_grant import DeviceGrant
from potluck_api.models.session import Session
from potluck_api.services.auth_service import AuthService
from potluck_api.services.device_auth import digest
from potluck_api.services.email_service import EmailService


async def grant_and_proof(client, monkeypatch):
    messages = []
    async def capture(self, email, link):
        messages.append((email, link))
    monkeypatch.setattr(EmailService, "send_device_approval", capture)
    response = await client.post("/auth/device/start")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    grant = response.json()
    response = await client.post("/auth/device/request-link", json={"user_code": grant["user_code"], "email": "CLI@Example.com"})
    assert response.status_code == 202
    assert messages[0][0] == "cli@example.com"
    assert "#token=" in messages[0][1]
    return grant, messages[0][1].split("#token=")[1]


async def test_device_flow_single_use_with_real_session(client, db, monkeypatch):
    grant, proof = await grant_and_proof(client, monkeypatch)
    stored = await db.scalar(select(DeviceGrant))
    assert stored.device_code_hash == digest(grant["device_code"])
    assert grant["device_code"] not in stored.device_code_hash
    assert (await client.post("/auth/device/review", json={"token": proof})).json() == {"email": "cli@example.com"}
    # Opening/reviewing an email must not approve a device or create a session.
    assert await db.scalar(select(func.count()).select_from(Session)) == 0
    assert (await client.post("/auth/device/approve", json={"token": proof, "user_code": grant["user_code"], "approved": True})).json() == {"status": "approved"}
    results = await asyncio.gather(*[client.post("/auth/device/token", json={"device_code": grant["device_code"]}) for _ in range(2)])
    assert sorted(r.status_code for r in results) == [200, 400]
    session = next(r.json() for r in results if r.status_code == 200)
    assert session["email"] == "cli@example.com"
    me = await client.get("/me", headers={"Authorization": "Bearer " + session["token"]})
    assert me.json()["id"] == session["memberId"]
    assert await db.scalar(select(func.count()).select_from(Session)) == 1
    assert (await client.post("/auth/device/approve", json={"token": proof, "user_code": grant["user_code"], "approved": True})).status_code == 400


async def test_pending_slowdown_and_expiry(client, db):
    grant = (await client.post("/auth/device/start")).json()
    pending = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
    assert pending.json()["error"] == "authorization_pending"
    slow = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
    assert slow.json() == {"error": "slow_down", "interval": 10}
    await db.execute(update(DeviceGrant).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await db.commit()
    assert (await client.post("/auth/device/token", json={"device_code": grant["device_code"]})).json()["error"] == "expired_token"


async def test_denied_wrong_code_and_wrong_token_purpose(client, db, monkeypatch):
    grant, proof = await grant_and_proof(client, monkeypatch)
    body = {"token": proof, "user_code": "WRNG-WRNG", "approved": True}
    assert (await client.post("/auth/device/approve", json=body)).json()["error"] == "invalid_user_code"
    # Separate JWT purposes: a browser approval cannot sign the desktop in.
    assert (await client.get("/auth/callback", params={"token": proof})).status_code == 400
    magic = AuthService(db).create_magic_link("cli@example.com").split("token=")[1]
    assert (await client.post("/auth/device/review", json={"token": magic})).status_code == 400
    body.update(user_code=grant["user_code"], approved=False)
    assert (await client.post("/auth/device/approve", json=body)).json()["status"] == "denied"
    assert (await client.post("/auth/device/token", json={"device_code": grant["device_code"]})).json()["error"] == "access_denied"
    assert await db.scalar(select(func.count()).select_from(Session)) == 0


async def test_invalid_code_never_sends_mail(client, monkeypatch):
    async def fail(*_args):
        raise AssertionError("must not send email")
    monkeypatch.setattr(EmailService, "send_device_approval", fail)
    result = await client.post("/auth/device/request-link", json={"user_code": "WRNG-WRNG", "email": "nobody@example.com"})
    assert result.status_code == 202


async def test_device_email_recipient_limit(client, monkeypatch):
    from potluck_api.middleware.rate_limit import limiter
    monkeypatch.setattr(limiter, "enabled", True)
    grant = (await client.post("/auth/device/start")).json()
    messages = []
    async def capture(_self, email, link):
        messages.append(email)
    monkeypatch.setattr(EmailService, "send_device_approval", capture)
    body = {"user_code": grant["user_code"], "email": "cli-rate-test@example.com"}
    for _ in range(3):
        assert (await client.post("/auth/device/request-link", json=body)).status_code == 202
    assert (await client.post("/auth/device/request-link", json=body)).status_code == 429
    assert len(messages) == 3
