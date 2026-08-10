from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from potluck_api.config import get_settings

router = APIRouter(tags=["client"])


class ClientVersionResponse(BaseModel):
    """Response body for GET /client/version.

    `latest` is the newest client build available; `minimum` is the oldest
    version still allowed to run — the desktop app hard-blocks itself with an
    "Update required" screen if its own version is below `minimum`.
    """

    latest: str
    minimum: str
    notesUrl: str | None = None


@router.get("/version", response_model=ClientVersionResponse)
async def get_client_version() -> ClientVersionResponse:
    """Return the client version handshake.

    Public, unauthenticated, no DB access — every desktop client polls this
    on launch, so it stays cheap. Values are a config decision (env
    overrides: CLIENT_LATEST_VERSION, CLIENT_MINIMUM_VERSION,
    CLIENT_NOTES_URL), not hardcoded here, so bumping the minimum supported
    version is a deploy config change, not a code change.
    """
    settings = get_settings()
    return ClientVersionResponse(
        latest=settings.client_latest_version,
        minimum=settings.client_minimum_version,
        notesUrl=settings.client_notes_url or None,
    )
