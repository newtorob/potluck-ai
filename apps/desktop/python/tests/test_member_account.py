import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services.member_account import MemberAccount, origin


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def account(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POTLUCK_CLOUD_API_URL", "https://accounts.example")
    value = MemberAccount()
    value.request = AsyncMock()
    value._handoff = Mock()
    return value


def grant():
    return {"device_code": "private-device-code-1234567890", "user_code": "BCDF-GHJK",
            "verification_uri": "https://www.example/auth/device", "expires_in": 900, "interval": 5}


def session():
    return {"token": "private-member-token", "memberId": "member-id", "email": "member@example.com",
            "expiresAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}


@pytest.mark.anyio
async def test_resumable_login_keeps_secrets_private_and_restores_session(account):
    account.request.return_value = (200, grant())
    first = await account.start_login()
    restarted = MemberAccount()
    restarted.request = AsyncMock(return_value=(200, session()))
    restarted._handoff = Mock()
    assert await restarted.start_login() == first
    assert "device_code" not in json.dumps(first)
    assert "private" not in json.dumps(first)
    result = await restarted.poll_login()
    assert result["state"] == "signed_in"
    assert "private" not in json.dumps(result)
    assert MemberAccount().session()["token"] == "private-member-token"
    restarted._handoff.assert_called_once_with("private-member-token")
    assert account.directory.stat().st_mode & 0o777 == 0o700
    assert (account.directory / "member-session.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.anyio
async def test_poll_backoff_persists_across_calls(account):
    account.request.side_effect = [(200, grant()), (400, {"error": "slow_down", "interval": 10})]
    await account.start_login()
    assert await account.poll_login() == {"state": "pending", "interval": 10}
    assert await account.poll_login() == {"state": "pending", "interval": 10}
    assert account.request.call_count == 2
    assert account.read("pending-device.json")["nextPollAt"] > time.time()


@pytest.mark.anyio
async def test_long_server_backoff_is_not_capped_to_a_faster_poll(account):
    account.request.side_effect = [(200, grant()), (400, {"error": "slow_down", "interval": 120})]
    await account.start_login()
    assert (await account.poll_login())["interval"] == 120
    assert account.read("pending-device.json")["nextPollAt"] > time.time() + 119


@pytest.mark.anyio
@pytest.mark.parametrize("error,state", [("access_denied", "denied"), ("expired_token", "expired")])
async def test_denied_or_expired_login_creates_no_session(account, error, state):
    account.request.side_effect = [(200, grant()), (400, {"error": error})]
    await account.start_login()
    assert await account.poll_login() == {"state": state}
    assert account.session() is None
    account._handoff.assert_not_called()


@pytest.mark.anyio
async def test_expired_local_grant_is_not_polled(account):
    account.write("pending-device.json", {"expiresAt": time.time() - 1})
    assert await account.poll_login() == {"state": "expired"}
    account.request.assert_not_called()


@pytest.mark.anyio
async def test_logout_tombstone_prevents_legacy_app_resurrection(account):
    account.write("member-session.json", session())
    account.request.return_value = (204, {})
    assert await account.logout() == {"signedIn": False, "revoked": True}
    account.request.assert_called_once_with("POST", "/auth/logout", token="private-member-token")
    account.request.reset_mock()
    assert (await account.commit("old-desktop-token", import_only=True))["signedIn"] is False
    account.request.assert_not_called()
    account._handoff.assert_called_with(None)


@pytest.mark.anyio
async def test_offline_logout_preserves_revocable_token_until_explicit_local_logout(account):
    account.write("member-session.json", session())
    account.request.side_effect = HTTPException(502, "offline")
    with pytest.raises(HTTPException):
        await account.logout()
    assert account.session()["token"] == "private-member-token"
    assert await account.logout(local_only=True) == {"signedIn": False, "revoked": False}
    assert account.session() is None


@pytest.mark.anyio
async def test_revoked_session_allows_fresh_login(account):
    account.write("member-session.json", session())
    account.request.side_effect = [(401, {}), (200, grant())]
    assert (await account.start_login())["state"] == "pending"
    assert account.session() is None
    account._handoff.assert_called_with(None)


def test_expired_session_is_signed_out(account):
    account.write("member-session.json", {**session(), "expiresAt": "2020-01-01T00:00:00+00:00"})
    assert account.status()["signedIn"] is False


@pytest.mark.anyio
async def test_private_and_origin_bound_storage(account):
    account.write("member-session.json", session())
    path = account.directory / "member-session.json"
    path.chmod(0o644)
    with pytest.raises(HTTPException) as error:
        await account.start_login()
    assert error.value.status_code == 403
    path.chmod(0o600)
    account.cloud = "https://different.example"
    with pytest.raises(HTTPException) as error:
        await account.start_login()
    assert error.value.status_code == 409
    account.request.assert_not_called()


def test_symlink_session_is_rejected(account, tmp_path):
    account._directory(create=True)
    target = tmp_path / "other"
    target.write_text(json.dumps(session()))
    (account.directory / "member-session.json").symlink_to(target)
    with pytest.raises(HTTPException) as error:
        account.session()
    assert error.value.status_code == 403


@pytest.mark.parametrize("value", ["http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?key=x"])
def test_server_origin_validation(value):
    with pytest.raises(HTTPException):
        origin(value)


def test_account_routes_always_require_control_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN", raising=False)
    client = TestClient(create_app(), client=("127.0.0.1", 1234))
    assert client.get("/account").status_code == 503
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "control-secret")
    client = TestClient(create_app(), client=("127.0.0.1", 1234))
    assert client.get("/account").status_code == 401
    response = client.get("/account", headers={"Authorization": "Bearer control-secret"})
    assert response.status_code == 200
    assert response.json()["signedIn"] is False


@pytest.mark.anyio
async def test_household_uses_configured_coordinator_with_member_bearer(account, monkeypatch):
    from potluck_local.services import settings_manager
    settings = settings_manager.PotluckSettings()
    settings.network.coordinator_url = "https://coordinator.example"
    monkeypatch.setattr(settings_manager, "get_settings", lambda: settings)
    account.write("member-session.json", session())
    account.request.return_value = (200, {"household": None, "machines": []})
    assert (await account.household("list"))["coordinatorUrl"] == "https://coordinator.example"
    account.request.assert_called_once_with("GET", "/mesh/households/current", body=None,
        token="private-member-token", base="https://coordinator.example")
