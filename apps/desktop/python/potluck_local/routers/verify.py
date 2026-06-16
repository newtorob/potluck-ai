"""Verification endpoints — sequence scoring for model-substitution detection.

POST /verify/score returns the teacher-forced log-probability the currently
loaded model assigns to a sequence. A verifier (anchor) and a peer-under-test
each score the SAME sequence O; if the peer claims model X but runs a cheaper
model Y, its logprob(O) won't match the anchor's (the "scoring challenge").

This endpoint is OBSERVE-ONLY infrastructure: it only computes a number. It
does not touch credits, escrow, or reputation, and nothing calls it
automatically. The accumulating detector that consumes these scores lives
coordinator-side and is gated behind calibration before it ever affects payout.

Privacy: scores a caller-supplied sequence under the local model; returns only
aggregate log-prob figures (no per-token data, no prompt/output capture here).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from potluck_local.services.runtime import get_runtime

logger = logging.getLogger(__name__)

router = APIRouter()


class ScoreRequest(BaseModel):
    text: str = Field(..., description="Sequence to score (the served output / challenge sequence O).")
    prefix: str | None = Field(
        None, description="Optional context to condition on; only `text` is scored."
    )
    max_tokens: int = Field(2048, ge=1, le=8192, description="Cap on scored tokens.")


class ScoreResponse(BaseModel):
    model_id: str | None
    scored_tokens: int
    sum_logprob: float
    avg_token_logprob: float | None
    bits_per_byte: float | None
    n_bytes: int


@router.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest) -> ScoreResponse:
    runtime = get_runtime()
    if not runtime.is_loaded():
        raise HTTPException(status_code=503, detail="No model loaded")
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")
    try:
        result = await runtime.score_sequence(
            req.text, prefix=req.prefix, max_tokens=req.max_tokens
        )
    except Exception as exc:  # noqa: BLE001 — surface as 500 with a short reason
        logger.exception("score_failed")
        raise HTTPException(status_code=500, detail=f"score failed: {type(exc).__name__}")
    return ScoreResponse(**result)
