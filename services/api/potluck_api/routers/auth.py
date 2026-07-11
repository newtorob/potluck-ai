# NOTE: intentionally NOT using `from __future__ import annotations` here.
# slowapi's @limiter.limit wraps the endpoint with functools.wraps, which copies
# the stringized annotations but not this module's globals. With deferred
# annotations FastAPI then can't resolve `MagicLinkRequest` to a body model and
# misreads it as a query param (422). Real annotation objects avoid that.
from typing import Annotated

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.config import get_settings
from potluck_api.db.database import get_db
from potluck_api.middleware.auth import _extract_token
from potluck_api.middleware.rate_limit import limiter
from potluck_api.services.auth_service import AuthService, MagicLinkError
from potluck_api.services.email_service import EmailService

router = APIRouter(tags=["auth"])


class MagicLinkRequest(BaseModel):
    """Request body for POST /auth/request-link."""

    email: EmailStr


# ---------------------------------------------------------------------------
# POST /auth/request-link
# ---------------------------------------------------------------------------


@router.post("/request-link", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("10/minute")
async def request_magic_link(
    request: Request,  # required by slowapi
    body: MagicLinkRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Send a magic-link email to the provided address.

    Rate-limited to 10 requests per minute per IP to prevent email flooding.
    Always returns 202 regardless of whether the address maps to an existing
    member — we do not leak account existence.
    """
    link = AuthService(db).create_magic_link(body.email)
    await EmailService().send_magic_link(body.email, link)
    return {"status": "sent"}


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


@router.get("/callback")
async def magic_link_callback(
    response: Response,
    token: str = Query(..., description="JWT magic-link token from the email"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Verify a magic-link token, create/find the member, issue a session.

    Sets the HTTP-only session cookie and returns the session metadata
    (matching the frontend ``Session`` type: token / expiresAt / memberId).
    """
    settings = get_settings()
    try:
        member, raw_token, session = await AuthService(db).verify_magic_link(token)
    except MagicLinkError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.session_expiry_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_secure(),
        samesite=settings.cookie_samesite(),
        path="/",
    )
    return {
        "token": raw_token,
        "expiresAt": session.expires_at.isoformat(),
        "memberId": str(member.id),
    }


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def logout(
    request: Request,
    potluck_session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Invalidate the current session and clear the cookie.

    Accepts the session via the cookie (web) or ``Authorization: Bearer`` (the
    desktop app), so a Bearer-based client's server-side session is genuinely
    revoked, not just its (nonexistent) cookie cleared.
    """
    settings = get_settings()
    token = _extract_token(potluck_session, authorization)
    if token:
        await AuthService(db).revoke_session(token)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(settings.session_cookie_name, path="/")
    return resp
