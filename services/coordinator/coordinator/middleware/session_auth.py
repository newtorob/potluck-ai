"""Per-session member-identity resolution for the coordinator.

Without session auth every coordinator endpoint attributes work to the
dev-fixture ``STUB_MEMBER_ID`` (see ``coordinator.constants``). This module
resolves the member behind the caller's login session instead.

How identity crosses the two services
-------------------------------------
The cloud API (``services/api``) owns login: magic-link -> a random session
token, of which it stores only ``SHA-256(token)`` in the ``sessions`` table
(``token_hash``, ``member_id``, ``expires_at``). Both services default to the
*same* Postgres DSN (``services/api/config.py`` and
``services/coordinator/config.py`` are byte-identical on ``database_url``), so
the coordinator validates a session by hashing the presented token and reading
that shared table directly. It never mints or refreshes sessions; it is a
read-only consumer of the API's login state. We use a raw ``SELECT`` rather
than a mapped model on purpose: the ``sessions`` table is owned by the API's
Alembic migrations, and giving the coordinator its own ``Session`` model would
make its Alembic think it owns the table too.

Gating
------
``POTLUCK_SESSION_AUTH_ENABLED`` is the master switch (mirrored by the
``member_session_auth`` preflight check):

* unset / not ``"1"`` -> dev passthrough. Every caller resolves to
  ``STUB_MEMBER_ID``. This is the dev posture and what the test suite
  runs under, so credits/jobs still attribute to the fixture member.
* ``"1"`` -> real auth. A valid, unexpired ``potluck_session`` cookie (or
  ``Authorization: Bearer`` token) is required; anything else is 401.

Transport mirrors the cloud API's ``get_current_member`` exactly: cookie name
``potluck_session``, bearer fallback for the desktop app where third-party
cookies against the coordinator origin are awkward.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.db.database import get_db

# Cookie the web client sets; matches services/api AuthService.
SESSION_COOKIE_NAME = "potluck_session"


def session_auth_enabled() -> bool:
    """Return True when per-session member auth is switched on.

    Single source of truth for the ``POTLUCK_SESSION_AUTH_ENABLED`` flag,
    shared with the preflight ``member_session_auth`` check so the boot-time
    banner and the request-time behaviour can never disagree.
    """
    return os.environ.get("POTLUCK_SESSION_AUTH_ENABLED", "").strip() == "1"


def _extract_token(cookie_value: str | None, auth_header: str | None) -> str | None:
    """Prefer the session cookie; fall back to an ``Authorization: Bearer`` token.

    The cookie is the primary transport for the web client. The bearer fallback
    exists for the desktop app, which presents the raw token from
    ``/auth/callback`` as a bearer credential instead of relying on a
    cross-origin cookie. Mirrors ``services/api`` ``_extract_token``.
    """
    if cookie_value:
        return cookie_value
    if auth_header and auth_header.lower().startswith("bearer "):
        candidate = auth_header[len("bearer ") :].strip()
        return candidate or None
    return None


async def resolve_session_member_id(
    potluck_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> uuid.UUID:
    """FastAPI dependency returning the caller's member_id.

    Dev passthrough (``POTLUCK_SESSION_AUTH_ENABLED`` unset) returns
    ``STUB_MEMBER_ID`` without touching the DB, so the coordinator behaves
    exactly as it did before auth landed.

    With auth enabled, the presented token is SHA-256 hashed and matched
    against an unexpired row in the shared ``sessions`` table.

    Raises:
        HTTPException 401 - auth is enabled but the session is missing,
        expired, or unknown.
    """
    if not session_auth_enabled():
        return STUB_MEMBER_ID

    token = _extract_token(potluck_session, authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    member_id = await db.scalar(
        text(
            "SELECT member_id FROM sessions "
            "WHERE token_hash = :token_hash AND expires_at > now() "
            "LIMIT 1"
        ),
        {"token_hash": token_hash},
    )
    if member_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    # asyncpg returns a uuid.UUID for a uuid column; coerce defensively so the
    # return type is honest regardless of driver.
    return member_id if isinstance(member_id, uuid.UUID) else uuid.UUID(str(member_id))


async def resolve_member_from_bearer(
    db: AsyncSession, authorization: str | None
) -> uuid.UUID | None:
    """WebSocket-side twin of ``resolve_session_member_id``.

    Returns the member behind a valid ``Authorization: Bearer`` session
    token, or None when session auth is disabled, the header is absent or
    malformed, or the session is unknown or expired. Never raises: the
    caller decides what "no session" means (for ``/peers/connect`` it falls
    back to the shared coordinator token, mirroring ``node_auth``).
    """
    if not session_auth_enabled():
        return None
    token = _extract_token(None, authorization)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    member_id = await db.scalar(
        text(
            "SELECT member_id FROM sessions "
            "WHERE token_hash = :token_hash AND expires_at > now() "
            "LIMIT 1"
        ),
        {"token_hash": token_hash},
    )
    if member_id is None:
        return None
    return member_id if isinstance(member_id, uuid.UUID) else uuid.UUID(str(member_id))
