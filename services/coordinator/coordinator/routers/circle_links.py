"""Direct circle links: the requester engine's three calls.

  POST   /me/circles/{circle_id}/links                       make a link
  POST   /me/circles/{circle_id}/links/{link_id}/renew       keep it alive
  DELETE /me/circles/{circle_id}/links/{link_id}             end it

Auth is the caller's member session (resolve_session_member_id), exactly like
routers/circles.py; in development every caller is the stub member. All the
work, and the reasoning behind each error code, is in services/circle_links.py.
The wire shapes are pinned to tests/fixtures/circle_link_shapes.json.

Kill switch: with POTLUCK_CIRCLE_LINKS_ENABLED off (the default) every route
here answers 503 links_disabled, before the session or the body is looked at.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.schemas.circle_link import (
    CircleLinkCreateRequest,
    CircleLinkCreateResponse,
    CircleLinkRenewResponse,
)
from coordinator.services import circle_links


async def require_links_enabled() -> None:
    if not circle_links.links_enabled():
        raise circle_links.link_error(
            503, "links_disabled", "Direct circle links are not available on this network."
        )


# The switch is a router-level dependency so it runs ahead of everything else.
router = APIRouter(tags=["circle-links"], dependencies=[Depends(require_links_enabled)])


class CircleLinkRenewRequest(BaseModel):
    """Body of a renew: empty today. Unknown keys are ignored so a later client
    can send more."""

    model_config = ConfigDict(extra="ignore")


@router.post(
    "/me/circles/{circle_id}/links",
    response_model=CircleLinkCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_circle_link(
    circle_id: uuid.UUID,
    req: CircleLinkCreateRequest,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleLinkCreateResponse:
    """Make a direct link from one of the caller's machines to a lender's
    machine in the circle. The token in the answer is shown once."""
    return await circle_links.create_link(db, member_id=member_id, circle_id=circle_id, req=req)


@router.post(
    "/me/circles/{circle_id}/links/{link_id}/renew",
    response_model=CircleLinkRenewResponse,
)
async def renew_circle_link(
    circle_id: uuid.UUID,
    link_id: uuid.UUID,
    body: CircleLinkRenewRequest | None = None,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> CircleLinkRenewResponse:
    """Extend a live link. 410 once it has ended; the caller must not retry."""
    return await circle_links.renew_link(
        db, member_id=member_id, circle_id=circle_id, link_id=link_id
    )


@router.delete(
    "/me/circles/{circle_id}/links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def end_circle_link(
    circle_id: uuid.UUID,
    link_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    member_id: uuid.UUID = Depends(resolve_session_member_id),
) -> Response:
    """End a link now. 204 whether or not it was live."""
    await circle_links.end_link(db, member_id=member_id, circle_id=circle_id, link_id=link_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
