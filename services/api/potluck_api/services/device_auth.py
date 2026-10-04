"""Browser-approved CLI login. Polling/expiry follow RFC 8628 semantics.

This is Potluck's JSON API, not a general OAuth provider. Browser email proofs
have a separate JWT purpose; they cannot redeem as desktop magic links.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.models.device_grant import DeviceGrant
from potluck_api.models.member import Member
from potluck_api.services.auth_service import AuthService

TTL = 900
ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").upper()


class DeviceAuthError(ValueError):
    pass


class DeviceAuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def start(self) -> dict:
        now = datetime.now(timezone.utc)
        await self.db.execute(delete(DeviceGrant).where(DeviceGrant.expires_at < now - timedelta(days=1)))
        raw = secrets.token_urlsafe(32)
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        self.db.add(DeviceGrant(device_code_hash=digest(raw), user_code_hash=digest(code),
                                expires_at=now + timedelta(seconds=TTL), status="pending", interval=5))
        await self.db.commit()
        uri = get_settings().app_url.rstrip("/") + "/auth/device"
        return {"device_code": raw, "user_code": code[:4] + "-" + code[4:],
                "verification_uri": uri, "expires_in": TTL, "interval": 5}

    async def email_link(self, code: str, email: str) -> str:
        grant = await self.db.scalar(select(DeviceGrant).where(
            DeviceGrant.user_code_hash == digest(normalize_code(code))))
        if not grant or grant.expires_at <= datetime.now(timezone.utc) or grant.status != "pending":
            raise DeviceAuthError("expired_token")
        token = jwt.encode({"type": "device_approval", "grant": str(grant.id),
                            "email": email.strip().lower(), "exp": grant.expires_at},
                           get_settings().magic_link_secret, algorithm="HS256")
        # Fragment never goes to web-server access logs or Referer headers.
        return get_settings().app_url.rstrip("/") + "/auth/device#token=" + token

    def proof(self, token: str) -> dict:
        try:
            payload = jwt.decode(token, get_settings().magic_link_secret, algorithms=["HS256"],
                                 options={"require": ["exp", "type", "grant", "email"]})
            if payload["type"] != "device_approval" or not isinstance(payload["email"], str):
                raise ValueError()
            uuid.UUID(payload["grant"])
            return payload
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
            raise DeviceAuthError("invalid_proof") from exc

    async def approve(self, token: str, code: str, approved: bool) -> dict:
        proof = self.proof(token)
        grant = await self.db.scalar(select(DeviceGrant).where(
            DeviceGrant.id == uuid.UUID(proof["grant"])).with_for_update())
        if not grant or grant.expires_at <= datetime.now(timezone.utc):
            raise DeviceAuthError("expired_token")
        if grant.status != "pending":
            raise DeviceAuthError("already_used")
        if not secrets.compare_digest(grant.user_code_hash, digest(normalize_code(code))):
            raise DeviceAuthError("invalid_user_code")
        if approved:
            member = await AuthService(self.db).member_for_email(proof["email"])
            grant.member_id = member.id
        grant.status = "approved" if approved else "denied"
        await self.db.commit()
        return {"status": grant.status}

    async def poll(self, device_code: str) -> dict:
        grant = await self.db.scalar(select(DeviceGrant).where(
            DeviceGrant.device_code_hash == digest(device_code)).with_for_update())
        now = datetime.now(timezone.utc)
        if not grant or grant.expires_at <= now:
            return {"error": "expired_token"}
        if grant.status == "denied":
            return {"error": "access_denied"}
        if grant.status == "consumed":
            return {"error": "invalid_grant"}
        if grant.last_polled_at and (now - grant.last_polled_at).total_seconds() < grant.interval:
            grant.interval += 5
            grant.last_polled_at = now
            await self.db.commit()
            return {"error": "slow_down", "interval": grant.interval}
        grant.last_polled_at = now
        if grant.status != "approved":
            await self.db.commit()
            return {"error": "authorization_pending", "interval": grant.interval}
        member = await self.db.get(Member, grant.member_id)
        if member is None:
            return {"error": "access_denied"}
        raw, session = AuthService(self.db).issue_session(member)
        grant.status = "consumed"
        await self.db.commit()
        return {"token": raw, "memberId": str(member.id), "email": member.email,
                "expiresAt": session.expires_at.isoformat()}
