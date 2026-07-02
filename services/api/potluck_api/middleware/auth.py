from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db
from potluck_api.models.member import Member
from potluck_api.services.auth_service import AuthService


def _extract_token(cookie_value: str | None, auth_header: str | None) -> str | None:
    """Prefer the session cookie; fall back to an Authorization: Bearer token.

    The cookie is the primary transport for the web client. The bearer fallback
    exists for the desktop app, where third-party cookies against
    api.trypotluck.ai are awkward — it can present the raw token returned by
    /auth/callback as a bearer credential instead.
    """
    if cookie_value:
        return cookie_value
    if auth_header and auth_header.lower().startswith("bearer "):
        candidate = auth_header[len("bearer ") :].strip()
        return candidate or None
    return None


async def get_current_member(
    potluck_session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> Member:
    """FastAPI dependency that validates the session and returns the Member.

    The session value is an opaque random token. AuthService SHA-256 hashes it
    and looks up the matching unexpired row in the sessions table.

    Cookie name: ``potluck_session``. Bearer fallback: ``Authorization: Bearer``.

    Raises:
        HTTPException 401 — missing, expired, or invalid session.
    """
    token = _extract_token(potluck_session, authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    member = await AuthService(db).validate_session(token)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    return member


async def require_admin(
    member: Annotated[Member, Depends(get_current_member)],
) -> Member:
    """Dependency that further asserts the authenticated member is an admin.

    Fail-closed: a non-admin gets 403. This stays a real check so admin routes
    are never silently exposed to every authenticated member.
    """
    if not getattr(member, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return member
