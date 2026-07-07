from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from potluck_api.middleware.auth import get_current_member
from potluck_api.models.member import Member
from potluck_api.services.discourse_service import (
    DiscourseConnectError,
    DiscourseService,
)

router = APIRouter(tags=["discourse_sso"])


# ---------------------------------------------------------------------------
# GET /auth/discourse-sso
# ---------------------------------------------------------------------------


@router.get("/discourse-sso")
async def discourse_sso(
    member: Annotated[Member, Depends(get_current_member)],
    sso: str = Query(..., description="Base64-encoded SSO payload from Discourse"),
    sig: str = Query(..., description="HMAC-SHA256 signature of the sso payload"),
) -> RedirectResponse:
    """DiscourseConnect (SSO) endpoint.

    Discourse redirects the user here after clicking "Log In" on the forum. The
    member must already have a valid Potluck session (get_current_member returns
    401 otherwise); the desktop/web app signs the member in before initiating
    the forum handshake. On success we verify the inbound signature, bind the
    forum identity to the Potluck member (external_id = member.id), and 302 back
    to Discourse with the signed response.
    """
    try:
        redirect_url = DiscourseService().build_login_redirect(sso, sig, member)
    except DiscourseConnectError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)
