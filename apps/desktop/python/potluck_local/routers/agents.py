"""Agent observability router.

Exposes read-only views of mcp_sessions and mcp_tool_calls so the desktop app
can show which coding agents are connected, what model each is using, and a
real-time feed of tool calls.

Mount at /agents in app.py.

Endpoints:
  GET /agents/sessions?include_disconnected=false&limit=100
  GET /agents/sessions/{id}
  GET /agents/activity?limit=100&client=&project=&tool=
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from potluck_local.services.memory_db import (
    get_session,
    list_recent_tool_calls,
    list_sessions,
    session_tool_calls,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _deserialize_session(row: dict[str, Any]) -> dict[str, Any]:
    """Convert peer_mcps from JSON string to list in-place."""
    if isinstance(row.get("peer_mcps"), str):
        try:
            row["peer_mcps"] = json.loads(row["peer_mcps"])
        except Exception:
            row["peer_mcps"] = []
    return row


def _deserialize_tool_call(row: dict[str, Any]) -> dict[str, Any]:
    """Convert args_summary from JSON string to dict in-place."""
    if isinstance(row.get("args_summary"), str):
        try:
            row["args_summary"] = json.loads(row["args_summary"])
        except Exception:
            row["args_summary"] = {}
    return row


@router.get("/sessions")
async def get_sessions(
    include_disconnected: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    """Return active agent sessions with tool_call_count.

    include_disconnected: when true, also returns sessions that have ended.
    """
    try:
        rows = await list_sessions(
            include_disconnected=include_disconnected,
            limit=limit,
        )
    except Exception as exc:
        logger.error("agents_sessions_error type=%s", type(exc).__name__)
        return []
    return [_deserialize_session(r) for r in rows]


@router.get("/sessions/{session_id}")
async def get_session_detail(session_id: str) -> dict:
    """Return a single session with its most recent tool calls (up to 200)."""
    try:
        row = await get_session(session_id)
    except Exception as exc:
        logger.error("agents_session_detail_error id=%s type=%s", session_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc

    if row is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    session = _deserialize_session(row)

    try:
        calls = await session_tool_calls(session_id, limit=200)
    except Exception as exc:
        logger.error("agents_session_calls_error id=%s type=%s", session_id, type(exc).__name__)
        calls = []

    session["recent_tool_calls"] = [_deserialize_tool_call(c) for c in calls]
    return session


@router.get("/activity")
async def get_activity(
    limit: int = Query(default=100, ge=1, le=1000),
    client: str | None = Query(default=None),
    project: str | None = Query(default=None),
    tool: str | None = Query(default=None),
) -> list[dict]:
    """Return recent tool calls across all sessions, with optional filters."""
    try:
        rows = await list_recent_tool_calls(
            limit=limit,
            client=client,
            project=project,
            tool_name=tool,
        )
    except Exception as exc:
        logger.error("agents_activity_error type=%s", type(exc).__name__)
        return []
    return [_deserialize_tool_call(r) for r in rows]
