"""Usage dashboard router.

Read-only aggregations over the local `usage_events` table, plus a single
frontend-driven write (POST /usage/record) used for in-app network chat, whose
completion metadata only exists in the renderer (that path streams renderer ->
coordinator directly and never touches the sidecar; every other path inserts
server-side at its own completion point).

Mount at /usage in app.py. All endpoints are loopback-only + sidecar-token
gated by the app's peer_access_middleware, and none are ever proxied to the
coordinator. Everything reported is metadata only (tokens, latency, scope,
model, ok/fail); no prompt or response content is stored or returned, and
served rows carry no peer identity.

Endpoints:
  GET  /usage/summary?window=30
  GET  /usage/activity?range=365
  GET  /usage/models?limit=8
  GET  /usage/pace
  GET  /usage/split?window=30
  POST /usage/record        (in-app network chat completion, from the frontend)
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from potluck_local.services import usage_store

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/dashboard')
async def get_dashboard(window: int = Query(default=7, ge=1, le=90),
                        direction: Literal['consumed', 'served'] = 'consumed',
                        household: bool = False) -> dict:
    if window not in (1, 7, 30, 90):
        raise HTTPException(422, "window must be 1, 7, 30, or 90 days")
    from potluck_local.services.usage_dashboard import dashboard
    return await dashboard(window, direction, household)


@router.get('/performance')
async def get_performance(household: bool = False) -> dict:
    from potluck_local.services.performance_status import performance_status
    return await performance_status(include_household=household)


class UsageRecord(BaseModel):
    """One completed request reported by the frontend for the paths that do not
    pass through the sidecar (in-app circle / pool / my_machines chat). Metadata
    only, by contract: no prompt or response text is accepted here."""

    direction: str = Field(default="consumed")
    scope: str = Field(default="local")
    model_slug: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    ttft_ms: int | None = None
    duration_ms: int | None = None
    ok: bool = True
    error_class: str | None = None


@router.get("/summary")
async def get_summary(window: float = Query(default=30.0, ge=1, le=3650)) -> dict:
    return await usage_store.summary(window_days=window)


@router.get("/activity")
async def get_activity(range: int = Query(default=365, ge=1, le=3650)) -> list[dict]:
    return await usage_store.activity(range_days=range)


@router.get("/models")
async def get_models(limit: int = Query(default=8, ge=1, le=50)) -> list[dict]:
    return await usage_store.top_models(limit=limit)


@router.get("/pace")
async def get_pace() -> dict:
    return await usage_store.pace()


@router.get("/split")
async def get_split(window: float = Query(default=30.0, ge=1, le=3650)) -> dict:
    return await usage_store.split(window_days=window)


@router.post("/record")
async def post_record(record: UsageRecord) -> dict:
    """Persist a completed in-app request the sidecar could not observe itself.
    Fire-and-forget from the caller's view; always returns ok."""
    await usage_store.record_usage_event(
        direction=record.direction,
        scope=record.scope,
        model_slug=record.model_slug,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        ttft_ms=record.ttft_ms,
        duration_ms=record.duration_ms,
        ok=record.ok,
        error_class=record.error_class,
    )
    return {"ok": True}
