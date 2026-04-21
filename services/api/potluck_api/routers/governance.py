from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from potluck_api.middleware.auth import get_current_member

router = APIRouter(tags=["governance"])


class VoteRequest(BaseModel):
    """Body for POST /votes/{proposal_id}."""

    choice: str  # e.g. "yes", "no", "abstain"


# ---------------------------------------------------------------------------
# GET /proposals
# ---------------------------------------------------------------------------


@router.get("/proposals")
async def list_proposals() -> list[object]:
    """Return all active governance proposals.

    Returns an empty list until the governance system is implemented in
    Task 9 (governance_service.py).
    """
    # TODO (Task 9): return await GovernanceService.list_active()
    return []


# ---------------------------------------------------------------------------
# POST /votes/{proposal_id}
# ---------------------------------------------------------------------------


@router.post("/votes/{proposal_id}", status_code=status.HTTP_201_CREATED)
async def cast_vote(
    proposal_id: str,
    body: VoteRequest,
    member: Annotated[object, Depends(get_current_member)],
) -> dict[str, object]:
    """Cast or update the authenticated member's vote on a proposal.

    Full implementation in Task 9.
    """
    # TODO (Task 9): call GovernanceService.cast_vote(member, proposal_id, body.choice)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Voting not yet implemented — see Task 9",
    )
