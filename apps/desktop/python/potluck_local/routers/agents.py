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

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from potluck_local.services.memory_db import (
    delete_disconnected_sessions,
    delete_session,
    get_session,
    list_recent_tool_calls,
    list_sessions,
    session_tool_calls,
)

from potluck_local.services.memory_safety import redact_credentials

logger = logging.getLogger(__name__)
router = APIRouter()


def _memory_preview(content: str) -> str:
    # Redact before truncating, including credentials spanning the cutoff.
    safe = redact_credentials(content)
    return safe[:200] + ("…" if len(safe) > 200 else "")


def _deserialize_session(row: dict[str, Any]) -> dict[str, Any]:
    """Convert peer_mcps from JSON string to list in-place and compute the
    liveness tier ('live' | 'idle' | 'stale' | 'disconnected') so the UI
    can render the four states without re-doing time math in JS."""
    if isinstance(row.get("peer_mcps"), str):
        try:
            row["peer_mcps"] = json.loads(row["peer_mcps"])
        except Exception:
            row["peer_mcps"] = []
    row["liveness"] = _liveness(row)
    return row


def _liveness(row: dict[str, Any]) -> str:
    """Bucket the row into one of four liveness tiers:
        - 'disconnected' — disconnected_at is set
        - 'live'         — last_seen_at within last 60 seconds (heartbeating)
        - 'idle'         — last_seen_at within 5 min (connected, quiet)
        - 'stale'        — last_seen_at older than 5 min, but not yet swept
    Computing this server-side keeps the AgentsView render simple and
    avoids client/server clock-skew weirdness on the boundaries."""
    if row.get("disconnected_at") is not None:
        return "disconnected"
    last_seen = row.get("last_seen_at")
    if not last_seen:
        return "stale"
    try:
        from datetime import datetime, timezone
        last_seen_dt = datetime.fromisoformat(last_seen)
        if last_seen_dt.tzinfo is None:
            last_seen_dt = last_seen_dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_seen_dt).total_seconds()
    except Exception:
        return "stale"
    if age < 60:
        return "live"
    if age < 300:
        return "idle"
    return "stale"


def _deserialize_tool_call(row: dict[str, Any]) -> dict[str, Any]:
    """Convert args_summary and memory_ids_touched from JSON strings to
    in-memory shapes. Both columns are nullable; pre-2026-05-20 rows have
    NULL memory_ids_touched and that propagates as null in the response.
    """
    if isinstance(row.get("args_summary"), str):
        try:
            row["args_summary"] = json.loads(row["args_summary"])
        except Exception:
            row["args_summary"] = {}
    if isinstance(row.get("memory_ids_touched"), str):
        try:
            row["memory_ids_touched"] = json.loads(row["memory_ids_touched"])
        except Exception:
            row["memory_ids_touched"] = None
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


TRACE_FORMAT_VERSION = "potluck-trace-v1"


@router.get("/sessions/{session_id}/trace")
async def export_session_trace(session_id: str) -> Response:
    """Download a portable trace of one session for bug reports.

    The blob is JSON: session metadata + ALL tool calls (not capped at 200)
    + memory previews for any memory ids touched. Same privacy guarantees
    as the rest of the agent observability surface — no prompt content, no
    raw memory content beyond the first 200 chars per memory. Safe to
    attach to a public bug tracker.

    Opt-in: the user clicks Export in the debugger drawer. We never
    auto-upload anywhere.

    Filename pattern: potluck-trace-{session_short}-{YYYYMMDD-HHMMSS}.json
    """
    from datetime import datetime, timezone

    try:
        row = await get_session(session_id)
    except Exception as exc:
        logger.error("agents_trace_error id=%s type=%s", session_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    session = _deserialize_session(row)

    # No 200-row cap on exports — the user is asking for the whole picture.
    try:
        calls = await session_tool_calls(session_id, limit=10_000)
    except Exception as exc:
        logger.error("agents_trace_calls_error id=%s type=%s", session_id, type(exc).__name__)
        calls = []
    tool_calls = [_deserialize_tool_call(c) for c in calls]

    # Collect all unique memory ids touched across the calls and resolve
    # them to previews in one batch. Deleted memories are silently dropped
    # (the consumer can spot them by id mismatch).
    touched: set[str] = set()
    for c in tool_calls:
        ids = c.get("memory_ids_touched")
        if isinstance(ids, list):
            touched.update(i for i in ids if isinstance(i, str) and i)

    memories: list[dict[str, Any]] = []
    if touched:
        import aiosqlite
        from potluck_local.services.memory_db import get_memory_db_path

        ids_list = list(touched)
        placeholders = ",".join(["?"] * len(ids_list))
        try:
            async with aiosqlite.connect(get_memory_db_path()) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    f"""
                    SELECT id, content, category, project, created_at, is_active
                    FROM memories
                    WHERE id IN ({placeholders})
                    """,
                    ids_list,
                )
                rows = await cursor.fetchall()
            memories = [
                {
                    "id": r["id"],
                    "preview": _memory_preview(r["content"]),
                    "category": r["category"],
                    "project": r["project"],
                    "created_at": r["created_at"],
                    "is_active": bool(r["is_active"]),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.error("agents_trace_memories_error type=%s", type(exc).__name__)
            memories = []

    blob = {
        "format": TRACE_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "tool_calls": tool_calls,
        "memories": memories,
    }

    short_id = session_id.split("-", 1)[0]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"potluck-trace-{short_id}-{ts}.json"

    return Response(
        content=json.dumps(blob, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


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


@router.delete("/sessions/{session_id}", status_code=204, response_class=Response)
async def delete_session_endpoint(session_id: str) -> Response:
    """Delete one session row + its tool calls. Used to clear stale or
    accidentally-created rows from the /agents view. The agent (Claude
    Code / Cursor / etc.) is unaffected; if it's still running it will
    re-register on its next MCP call.

    response_class=Response is required by FastAPI's strict-204 assertion
    (default JSONResponse allows a body; status 204 forbids one).
    """
    try:
        existed = await delete_session(session_id)
    except Exception as exc:
        logger.error("agents_session_delete_error id=%s type=%s", session_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc
    if not existed:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")
    return Response(status_code=204)


@router.delete("/sessions", status_code=200)
async def clear_disconnected_sessions() -> dict:
    """Delete every disconnected session + its tool calls. Active sessions
    (disconnected_at IS NULL) are left alone."""
    try:
        removed = await delete_disconnected_sessions()
    except Exception as exc:
        logger.error("agents_clear_disconnected_error type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc
    return {"removed": removed}


@router.post("/sessions/sweep", status_code=200)
async def sweep_sessions() -> dict:
    """Run the stale-session sweep on demand. Marks any active session
    whose last_seen_at is older than 5 minutes as disconnected, and
    DELETES rows that never identified themselves and have zero tool
    calls older than 30 minutes (the AgentsView clutter problem).

    Returns counts. Idempotent; safe to spam the button. The same sweep
    runs automatically every 60s, but the manual trigger is the
    fast-feedback path from the AgentsView "Clean up stale" affordance.
    """
    from potluck_local.services.agent_observability import AgentObserver
    try:
        observer = AgentObserver()
        result = await observer.sweep_stale_sessions(max_age_seconds=300)
    except Exception as exc:
        logger.error("agents_sweep_error type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Sweep failed") from exc
    return result


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


class MemoryPreviewRequest(BaseModel):
    """Typed request body for POST /agents/memory-previews.

    Malformed payloads (wrong type for ids, ids too long, non-string elements)
    now produce clear 422 responses from FastAPI's validation layer instead of
    being silently filtered or returning an opaque 400.
    """

    ids: list[str] = Field(default_factory=list, max_length=100)


@router.post("/memory-previews")
async def memory_previews(req: MemoryPreviewRequest) -> list[dict]:
    """Resolve a list of memory ids to lightweight previews for the
    debugger's memory-overlay panel.

    POST (not GET) because ids can be many and putting them on a query
    string is awkward. Body shape: {"ids": ["uuid-1", "uuid-2", ...]}.

    Returns one row per id that exists; missing ids are silently dropped
    (the debugger renders that as 'memory deleted since this call'). Each
    row includes only what the panel renders: id, a truncated content
    preview, category, project, created_at, is_active. Full content is
    never broadcast — the privacy invariant on agent observability holds.
    """
    ids = [i for i in req.ids if i]  # drop empty strings
    if not ids:
        return []

    import aiosqlite
    from potluck_local.services.memory_db import get_memory_db_path

    db_path = get_memory_db_path()
    placeholders = ",".join(["?"] * len(ids))
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT id, content, category, project, created_at, is_active
                FROM memories
                WHERE id IN ({placeholders})
                """,
                ids,
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        logger.error("memory_previews_error type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc

    return [
        {
            "id": r["id"],
            "preview": _memory_preview(r["content"]),
            "category": r["category"],
            "project": r["project"],
            "created_at": r["created_at"],
            "is_active": bool(r["is_active"]),
        }
        for r in rows
    ]
