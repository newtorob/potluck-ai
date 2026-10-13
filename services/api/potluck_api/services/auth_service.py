"""Magic link generation, JWT signing, session management.

Full implementation spec: guide Section 5.5.
Task: 6

Responsibilities:
- create_magic_link(email) -> str - signs a JWT, returns the full magic-link URL
- verify_magic_link(token) -> tuple[Member, str, Session] - decodes JWT,
  finds/creates the member, creates a session row, returns
  (member, opaque session token, session row)
- validate_session(session_token) -> Member | None - hashes the opaque token,
  looks up the unexpired session row, returns the Member
- revoke_session(session_token) -> None - deletes the matching session row
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.models.consumed_magic_link import ConsumedMagicLink
from potluck_api.models.member import Member
from potluck_api.models.session import Session

# The token's "type" claim - guards against a token minted for another purpose
# (e.g. discourse SSO) being replayed against the magic-link verifier.
_MAGIC_LINK_TYPE = "magic_link"


class MagicLinkError(ValueError):
    """Raised when a magic-link token is expired, malformed, or wrong-type.

    Callers map this to HTTP 400 - the message is safe to surface to the user.
    """


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.settings = get_settings()

    def create_magic_link(self, email: str) -> str:
        """Sign a short-lived JWT and return the full magic-link URL.

        The URL base is ``app_url + magic_link_path`` - both configurable so the
        delivery transport (a web verify page vs a ``potluck://`` deep link) can
        change without editing this code.

        Every token gets a unique ``jti``. verify_magic_link() records it in
        ``consumed_magic_links`` on redemption, so a second redemption of the
        same link (e.g. an email client prefetching the URL, or a shoulder-surfed
        link) fails even inside the token's validity window.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "email": email,
            "iat": now,
            "exp": now + timedelta(minutes=self.settings.magic_link_expiry_minutes),
            "type": _MAGIC_LINK_TYPE,
            "jti": uuid.uuid4().hex,
        }
        token = jwt.encode(
            payload, self.settings.magic_link_secret, algorithm="HS256"
        )
        base = f"{self.settings.app_url.rstrip('/')}{self.settings.magic_link_path}"
        return f"{base}?token={token}"

    async def verify_magic_link(self, token: str) -> tuple[Member, str, Session]:
        """Verify the JWT, find-or-create the member, mint a session.

        Returns ``(member, raw_session_token, session_row)``. The raw token is
        only ever returned here; the DB stores ``SHA-256(raw_token)`` so a
        database breach never yields usable session tokens.

        Single-use: the token's ``jti`` is inserted into ``consumed_magic_links``
        in the same transaction that creates the session. A second redemption
        hits a primary-key conflict on that insert and the whole transaction
        (including the session row) rolls back - so a replayed link never
        mints a session, even inside the 15-minute validity window.

        Raises:
            MagicLinkError - token expired, malformed, wrong type, or already
            redeemed.
        """
        try:
            payload = jwt.decode(
                token, self.settings.magic_link_secret, algorithms=["HS256"]
            )
        except jwt.ExpiredSignatureError as exc:
            raise MagicLinkError("Magic link expired") from exc
        except jwt.InvalidTokenError as exc:
            raise MagicLinkError("Invalid magic link") from exc

        if payload.get("type") != _MAGIC_LINK_TYPE:
            raise MagicLinkError("Invalid magic link")
        email = payload.get("email")
        jti = payload.get("jti")
        exp = payload.get("exp")
        if not email or not jti or not exp:
            raise MagicLinkError("Invalid magic link")
        email = email.strip().lower()

        member = await self.member_for_email(email)

        self.db.add(
            ConsumedMagicLink(
                jti=jti,
                expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
            )
        )

        raw_token, session = self.issue_session(member)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise MagicLinkError("Magic link already used") from exc
        return member, raw_token, session

    async def member_for_email(self, email: str) -> Member:
        """Resolve a verified email, sharing identity rules across sign-in flows."""
        email = email.strip().lower()
        member = await self.db.scalar(select(Member).where(func.lower(Member.email) == email))
        if member is None:
            # Independent device approvals may create the same account concurrently.
            await self.db.execute(insert(Member).values(id=uuid.uuid4(), email=email)
                                  .on_conflict_do_nothing())
            member = await self.db.scalar(select(Member).where(func.lower(Member.email) == email))
        assert member is not None
        member.is_admin = email in self.settings.admin_email_list()
        return member

    def issue_session(self, member: Member) -> tuple[str, Session]:
        """Stage a session in the caller's transaction; caller commits consumption."""
        raw_token = secrets.token_urlsafe(64)
        session = Session(member_id=member.id,
                          token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                          expires_at=datetime.now(timezone.utc) + timedelta(days=self.settings.session_expiry_days))
        self.db.add(session)
        return raw_token, session

    async def validate_session(self, raw_token: str) -> Member | None:
        """Return the Member for a valid, unexpired session token, else None."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        session = await self.db.scalar(
            select(Session).where(
                Session.token_hash == token_hash,
                Session.expires_at > datetime.now(timezone.utc),
            )
        )
        if session is None:
            return None
        return await self.db.get(Member, session.member_id)

    async def revoke_session(self, raw_token: str) -> None:
        """Delete the session row matching this token. Idempotent."""
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        session = await self.db.scalar(
            select(Session).where(Session.token_hash == token_hash)
        )
        if session is not None:
            await self.db.delete(session)
            await self.db.commit()
