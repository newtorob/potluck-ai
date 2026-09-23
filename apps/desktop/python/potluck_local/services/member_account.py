"""One private member-session store shared by the app and CLI on POSIX hosts."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from potluck_local.utils.paths import data_dir


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise HTTPException(400, "Use an HTTPS origin (HTTP is allowed only on loopback).")
    try:
        parsed.port
    except ValueError as exc:
        raise HTTPException(400, "Invalid server port") from exc
    return value.rstrip("/")


class MemberAccount:
    def __init__(self):
        self.directory = data_dir() / "auth"
        self.cloud = origin(os.environ.get("POTLUCK_CLOUD_API_URL", "https://api.trypotluck.ai"))
        self.lock = asyncio.Lock()

    def _directory(self, create: bool = False) -> bool:
        if os.name == "nt":
            raise HTTPException(501, "Shared member accounts require the Windows ACL adapter.")
        if create:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            info = self.directory.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HTTPException(403, "Account directory must be owned by you with permissions 0700.")
        return True

    def read(self, name: str) -> dict | None:
        if not self._directory():
            return None
        try:
            fd = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise HTTPException(403, "Account state cannot be opened safely.") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
                raise HTTPException(403, "Account state must be a private regular file owned by you.")
            with os.fdopen(fd, "r") as stream:
                fd = -1
                value = json.load(stream)
            if not isinstance(value, dict) or value.get("schemaVersion") != 1:
                raise ValueError()
            if value.get("cloudOrigin") != self.cloud:
                raise HTTPException(409, "Account belongs to a different cloud origin. Use its original configuration.")
            return value
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, "Account state is invalid; no credentials were sent.") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def write(self, name: str, value: dict) -> None:
        self._directory(create=True)
        fd, path = tempfile.mkstemp(prefix=".account-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({**value, "schemaVersion": 1, "cloudOrigin": self.cloud}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.directory / name)
        finally:
            Path(path).unlink(missing_ok=True)

    def session(self) -> dict | None:
        value = self.read("member-session.json")
        if not value or value.get("token") is None:
            return None
        if not all(isinstance(value.get(k), str) and value[k] for k in ("token", "memberId", "email")):
            raise HTTPException(409, "Account state is invalid; no credentials were sent.")
        if value.get("expiresAt"):
            try:
                expires = datetime.fromisoformat(value["expiresAt"].replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError()
                if expires <= datetime.now(timezone.utc):
                    return None
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(409, "Account expiry is invalid.") from exc
        return value

    def status(self) -> dict:
        session = self.session()
        return {"signedIn": bool(session), "memberId": session.get("memberId") if session else None,
                "email": session.get("email") if session else None,
                "expiresAt": session.get("expiresAt") if session else None}

    async def request(self, method: str, path: str, *, body: dict | None = None,
                      token: str | None = None, base: str | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, (base or self.cloud) + path, json=body, headers=headers) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise HTTPException(502, "Account service returned an oversized response.")
                    value = json.loads(raw) if raw else {}
                    if not isinstance(value, dict):
                        raise ValueError()
                    return response.status_code, value
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Account service is unavailable. Retry when connected.") from exc

    @staticmethod
    def require_success(status: int) -> None:
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            raise HTTPException(401, "Member session expired or invalid. Run potluck login.")
        raise HTTPException(502, "Account service rejected the request. Retry or check the server configuration.")

    def _handoff(self, token: str | None) -> None:
        from potluck_local.services.contributor_service import get_contributor_service
        get_contributor_service().set_member_session(token)

    async def commit(self, token: str, *, import_only: bool = False) -> dict:
        async with self.lock:
            # The signed-out tombstone prevents old app credentials resurrecting
            # an account after CLI logout. Import never changes an existing account.
            if import_only and self.read("member-session.json") is not None:
                return self.status()
            status, profile = await self.request("GET", "/me", token=token)
            self.require_success(status)
            if not isinstance(profile.get("id"), str) or not isinstance(profile.get("email"), str):
                raise HTTPException(502, "Account profile is invalid.")
            self.write("member-session.json", {"token": token, "memberId": profile["id"], "email": profile["email"]})
            self._handoff(token)
            return self.status()

    async def start_login(self) -> dict:
        async with self.lock:
            session = self.session()
            if session:
                status, _ = await self.request("GET", "/me", token=session["token"])
                if status in {401, 403}:
                    self.write("member-session.json", {"token": None})
                    self._handoff(None)
                else:
                    self.require_success(status)
                    return {"state": "signed_in", **self.status()}
            pending = self.read("pending-device.json")
            if not pending or pending.get("expiresAt", 0) <= time.time():
                status, grant = await self.request("POST", "/auth/device/start")
                if status == 404:
                    raise HTTPException(503, "This account server does not support device login yet.")
                self.require_success(status)
                try:
                    if not isinstance(grant["device_code"], str) or len(grant["device_code"]) < 20:
                        raise ValueError()
                    if not isinstance(grant["user_code"], str) or not 8 <= len(grant["user_code"]) <= 32:
                        raise ValueError()
                    verification = urlsplit(grant["verification_uri"])
                    origin(f"{verification.scheme}://{verification.netloc}")
                    if verification.query or verification.fragment:
                        raise ValueError()
                    pending = {"id": str(uuid.uuid4()), "deviceCode": grant["device_code"],
                               "userCode": grant["user_code"], "verificationUri": grant["verification_uri"],
                               "expiresAt": time.time() + min(1800, max(1, int(grant["expires_in"]))),
                               "interval": max(5, int(grant.get("interval", 5))), "nextPollAt": 0}
                except (KeyError, ValueError, TypeError) as exc:
                    raise HTTPException(502, "Device login response is invalid.") from exc
                self.write("pending-device.json", pending)
            return {"state": "pending", **{key: pending[key] for key in (
                "id", "userCode", "verificationUri", "expiresAt", "interval")}}

    async def poll_login(self) -> dict:
        async with self.lock:
            if self.session():
                return {"state": "signed_in", **self.status()}
            pending = self.read("pending-device.json")
            if not pending or pending.get("expiresAt", 0) <= time.time():
                return {"state": "expired"}
            if time.time() < pending["nextPollAt"]:
                return {"state": "pending", "interval": pending["interval"]}
            pending["nextPollAt"] = time.time() + pending["interval"]
            self.write("pending-device.json", pending)
            status, result = await self.request("POST", "/auth/device/token", body={"device_code": pending["deviceCode"]})
            if status == 400 and result.get("error") in {"authorization_pending", "slow_down"}:
                pending["interval"] = max(pending["interval"], int(result.get("interval", 5)))
                pending["nextPollAt"] = time.time() + pending["interval"]
                self.write("pending-device.json", pending)
                return {"state": "pending", "interval": pending["interval"]}
            if status == 400 and result.get("error") in {"access_denied", "expired_token", "invalid_grant"}:
                self.write("pending-device.json", {"expiresAt": 0})
                return {"state": "denied" if result["error"] == "access_denied" else "expired"}
            self.require_success(status)
            if not all(isinstance(result.get(k), str) and result[k] for k in ("token", "memberId", "email", "expiresAt")):
                raise HTTPException(502, "Device session response is invalid.")
            self.write("member-session.json", {k: result[k] for k in ("token", "memberId", "email", "expiresAt")})
            self.write("pending-device.json", {"expiresAt": 0})
            self._handoff(result["token"])
            return {"state": "signed_in", **self.status()}

    async def logout(self, *, local_only: bool = False) -> dict:
        async with self.lock:
            session = self.session()
            if session and not local_only:
                status, _ = await self.request("POST", "/auth/logout", token=session["token"])
                if status not in {401, 403}:
                    self.require_success(status)
            self.write("member-session.json", {"token": None})
            self.write("pending-device.json", {"expiresAt": 0})
            self._handoff(None)
            return {"signedIn": False, "revoked": bool(session) and not local_only}

    async def household(self, action: str, body: dict | None = None) -> dict:
        from potluck_local.services.settings_manager import get_settings
        session = self.session()
        if not session:
            raise HTTPException(401, "Run potluck login first.")
        base = origin(get_settings().network.coordinator_url or os.environ.get("POTLUCK_COORDINATOR_URL")
                      or "https://coordinator.trypotluck.ai")
        method, path = {"list": ("GET", "/mesh/households/current"),
                        "create": ("POST", "/mesh/households"),
                        "invite": ("POST", "/mesh/pairing-tokens")}[action]
        status, result = await self.request(method, path, body=body, token=session["token"], base=base)
        if status == 404 and action == "invite":
            raise HTTPException(409, "Create your household first: potluck household create.")
        self.require_success(status)
        return {**result, "coordinatorUrl": base}


_accounts: dict[tuple[str, str], MemberAccount] = {}


def get_member_account() -> MemberAccount:
    key = (str(data_dir()), os.environ.get("POTLUCK_CLOUD_API_URL", "https://api.trypotluck.ai"))
    if key not in _accounts:
        _accounts[key] = MemberAccount()
    return _accounts[key]
