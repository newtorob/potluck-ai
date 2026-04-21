from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

router = APIRouter(tags=["discourse_sso"])


# ---------------------------------------------------------------------------
# GET /auth/discourse-sso
# ---------------------------------------------------------------------------


@router.get("/discourse-sso")
async def discourse_sso(
    sso: str = Query(..., description="Base64-encoded SSO payload from Discourse"),
    sig: str = Query(..., description="HMAC-SHA256 signature of the sso payload"),
) -> object:
    """DiscourseConnect (SSO) endpoint.

    Discourse redirects the user here after clicking "Log In" on the forum.
    The handler must:
    1. Verify HMAC-SHA256(sso, DISCOURSE_SSO_SECRET) == sig.
    2. Decode the sso payload (base64 → query string → dict).
    3. Confirm the user is authenticated via their session cookie.
    4. Build a response payload with nonce, external_id, email, username.
    5. Sign and redirect back to DISCOURSE_URL/session/sso_login.

    Full implementation in Task 10 (discourse_service.py).
    """
    # TODO (Task 10): implement DiscourseService.handle_sso(sso, sig, member)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Discourse SSO not yet implemented — see Task 10",
    )
