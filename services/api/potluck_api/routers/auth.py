from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr

from potluck_api.middleware.rate_limit import limiter

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
) -> dict[str, str]:
    """Send a magic-link email to the provided address.

    Rate-limited to 10 requests per minute per IP to prevent email flooding.
    Full implementation in Task 6 (auth_service.py + email_service.py).
    """
    # TODO (Task 6): call AuthService.create_magic_link(body.email)
    #                then EmailService.send_magic_link(body.email, link)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Magic link sending not yet implemented — see Task 6",
    )


# ---------------------------------------------------------------------------
# GET /auth/callback
# ---------------------------------------------------------------------------


@router.get("/callback")
async def magic_link_callback(
    token: str = Query(..., description="JWT magic-link token from the email"),
) -> dict[str, str]:
    """Verify a magic-link token, create/find the member, issue a session cookie.

    Full implementation in Task 6.
    """
    # TODO (Task 6): call AuthService.verify_magic_link(token)
    #                set HttpOnly session cookie, redirect or return member JSON
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Magic link verification not yet implemented — see Task 6",
    )


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def logout(request: Request) -> Response:
    """Invalidate the current session.

    Full implementation in Task 6.
    """
    # TODO (Task 6): read potluck_session cookie, delete DB row, clear cookie
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Logout not yet implemented — see Task 6",
    )
