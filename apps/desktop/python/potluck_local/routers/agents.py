"""Agent observability router.

Exposes read-only views of mcp_sessions and mcp_tool_calls so the desktop app
can show which coding agents are connected, what model each is using, and a
real-time feed of tool calls.

Mount at /agents in app.py.

Endpoints:
  GET /agents/runs?status=&provider=&project=&limit=100
  GET /agents/runs/{id}
  GET /agents/runs/{id}/events?after_seq=0
  GET /agents/sessions?include_disconnected=false&limit=100
  GET /agents/sessions/{id}
  GET /agents/activity?limit=100&client=&project=&tool=
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from potluck_local.services.agent_connectors import (
    AgentConnectorError,
    connect_provider,
    connector_status,
    disconnect_provider,
)
from potluck_local.services.memory_db import (
    delete_disconnected_sessions,
    delete_session,
    get_session,
    get_agent_run,
    list_agent_events,
    list_agent_instances,
    list_agent_resources,
    list_agent_runs,
    list_recent_tool_calls,
    list_sessions,
    session_tool_calls,
)
from potluck_local.services.managed_agents import (
    ApprovalDecision,
    ManagedAgentError,
    SandboxMode,
    get_managed_agent_manager,
)
from potluck_local.services.task_state import TaskState, get_task_manager
from potluck_local.services.memory_safety import redact_credentials

from potluck_local.services.memory_safety import redact_credentials

logger = logging.getLogger(__name__)
router = APIRouter()


def _memory_preview(content: str) -> str:
    # Redact before truncating, including credentials spanning the cutoff.
    safe = redact_credentials(content)
    return safe[:200] + ("…" if len(safe) > 200 else "")


class AgentConnectorRequest(BaseModel):
    project_path: str = Field(min_length=1, max_length=4096)


class ManagedAgentStartRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=500)
    cwd: str = Field(min_length=1, max_length=4096)
    model: str | None = Field(default=None, max_length=200)
    sandbox: SandboxMode = "workspace-write"
    direction: str | None = Field(default=None, max_length=50_000)


class ManagedAgentMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50_000)

    @field_validator("text")
    @classmethod
    def _require_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank.")
        return value


class ManagedAgentApprovalRequest(BaseModel):
    decision: ApprovalDecision


@router.get("/connectors/{provider}")
async def get_connector_status(
    provider: Literal["codex", "claude"],
    project_path: str = Query(min_length=1, max_length=4096),
) -> dict[str, Any]:
    """Preview Potluck's project-local hook installation without writing."""
    try:
        return connector_status(project_path, provider)
    except AgentConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/connectors/{provider}/connect")
async def connect_agent_provider(
    provider: Literal["codex", "claude"], request: AgentConnectorRequest
) -> dict[str, Any]:
    """Merge Potluck-owned lifecycle hooks into an existing provider config."""
    try:
        return connect_provider(request.project_path, provider)
    except AgentConnectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/connectors/{provider}/disconnect")
async def disconnect_agent_provider(
    provider: Literal["codex", "claude"], request: AgentConnectorRequest
) -> dict[str, Any]:
    """Remove Potluck-owned hooks while preserving all unrelated config."""
    try:
        return disconnect_provider(request.project_path, provider)
    except AgentConnectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _task_prompt(task: TaskState, direction: str | None) -> str:
    """Build the first turn from durable task state without logging it."""
    lines = [
        f"Work on this Potluck task: {task.task_name}",
        f"Objective: {task.objective}",
    ]
    if task.known_constraints:
        lines.append("Constraints:")
        lines.extend(f"- {value}" for value in task.known_constraints)
    if task.completed_steps:
        lines.append("Completed steps:")
        lines.extend(f"- {value}" for value in task.completed_steps[-10:])
    if task.pending_steps:
        lines.append("Pending steps:")
        lines.extend(f"- {value}" for value in task.pending_steps)
    if task.context_summary:
        lines.append(f"Context: {task.context_summary}")
    if task.files_touched:
        lines.append(f"Files already involved: {', '.join(task.files_touched[-20:])}")
    if direction and direction.strip():
        lines.extend(("Current direction:", direction.strip()))
    lines.append(
        "Keep the Potluck task state current as you work. Complete task steps immediately "
        "and record new blockers or errors when they occur."
    )
    return "\n".join(lines)


def _managed_error(exc: ManagedAgentError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/managed/capabilities")
async def get_managed_capabilities() -> dict[str, Any]:
    """Report whether the local Codex CLI can host managed sessions."""
    return await get_managed_agent_manager().capabilities()


@router.get("/managed/sessions")
async def get_managed_sessions(
    task_id: str | None = Query(default=None, max_length=500),
) -> list[dict[str, Any]]:
    """Return workroom sessions retained by this Potluck process."""
    return get_managed_agent_manager().list_sessions(task_id=task_id)


@router.post("/managed/sessions", status_code=201)
async def start_managed_session(request: ManagedAgentStartRequest) -> dict[str, Any]:
    """Launch Codex against one durable task and repository folder."""
    task = await get_task_manager().get_task(request.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if task.status in {"completed", "abandoned"}:
        raise HTTPException(status_code=409, detail="Reopen this task before starting Codex.")
    try:
        return await get_managed_agent_manager().start_session(
            task_id=task.id,
            task_name=task.task_name,
            cwd=request.cwd,
            project=task.project,
            prompt=_task_prompt(task, request.direction),
            model=request.model.strip() if request.model and request.model.strip() else None,
            sandbox=request.sandbox,
        )
    except ManagedAgentError as exc:
        raise _managed_error(exc) from exc


@router.get("/managed/sessions/{session_id}")
async def get_managed_session(session_id: str) -> dict[str, Any]:
    try:
        return get_managed_agent_manager().get_session(session_id).public()
    except ManagedAgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/managed/sessions/{session_id}/messages")
async def send_managed_message(
    session_id: str, request: ManagedAgentMessageRequest
) -> dict[str, Any]:
    try:
        return await get_managed_agent_manager().send_message(session_id, request.text)
    except ManagedAgentError as exc:
        raise _managed_error(exc) from exc


@router.post("/managed/sessions/{session_id}/steer")
async def steer_managed_session(
    session_id: str, request: ManagedAgentMessageRequest
) -> dict[str, Any]:
    try:
        return await get_managed_agent_manager().steer(session_id, request.text)
    except ManagedAgentError as exc:
        raise _managed_error(exc) from exc


@router.post("/managed/sessions/{session_id}/interrupt")
async def interrupt_managed_session(session_id: str) -> dict[str, Any]:
    try:
        return await get_managed_agent_manager().interrupt(session_id)
    except ManagedAgentError as exc:
        raise _managed_error(exc) from exc


@router.post("/managed/sessions/{session_id}/approvals/{approval_id}")
async def resolve_managed_approval(
    session_id: str,
    approval_id: str,
    request: ManagedAgentApprovalRequest,
) -> dict[str, Any]:
    try:
        return await get_managed_agent_manager().resolve_approval(
            session_id, approval_id, request.decision
        )
    except ManagedAgentError as exc:
        raise _managed_error(exc) from exc


def _deserialize_agent_event(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("metadata"), str):
        try:
            row["metadata"] = json.loads(row["metadata"])
        except Exception:
            row["metadata"] = {}
    return row


@router.get("/runs")
async def get_runs(
    status: str | None = Query(default=None, max_length=50),
    provider: str | None = Query(default=None, max_length=50),
    project: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    """Return provider-native Codex and Claude execution runs."""
    try:
        return await list_agent_runs(
            status=status,
            provider=provider,
            project=project,
            limit=limit,
        )
    except Exception as exc:
        logger.error("agent_runs_error type=%s", type(exc).__name__)
        return []


@router.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> dict:
    """Return an ascending event delta suitable for cursor polling."""
    try:
        run = await get_agent_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
        instances = await list_agent_instances(run_id)
        rows = await list_agent_events(run_id, after_seq=after_seq, limit=limit)
        next_cursor = rows[-1]["seq"] if rows else after_seq
        resources = await list_agent_resources(
            run_id,
            after_seq=after_seq,
            through_seq=next_cursor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("agent_run_events_error id=%s type=%s", run_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc
    events = [_deserialize_agent_event(row) for row in rows]
    return {
        "instances": instances,
        "events": events,
        "resources": resources,
        "next_cursor": next_cursor,
    }


@router.get("/runs/{run_id}")
async def get_run_detail(run_id: str) -> dict:
    """Return one run, its lanes, and the first event cursor page."""
    try:
        run = await get_agent_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
        instances = await list_agent_instances(run_id)
        rows = await list_agent_events(run_id, limit=1000)
        next_cursor = rows[-1]["seq"] if rows else 0
        resources = await list_agent_resources(run_id, through_seq=next_cursor)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("agent_run_detail_error id=%s type=%s", run_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Database error") from exc

    events = [_deserialize_agent_event(row) for row in rows]
    return {
        "run": run,
        "instances": instances,
        "events": events,
        "resources": resources,
        "next_cursor": next_cursor,
    }


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


TRACE_FORMAT_VERSION = "potluck-trace-v2"


@router.get("/sessions/{session_id}/trace")
async def export_session_trace(
    session_id: str,
    mode: Literal["sanitized", "local-diagnostic"] = Query(default="sanitized"),
) -> Response:
    """Download a portable trace of one session for bug reports.

    Sanitized is the default and contains metadata only. Local diagnostic is
    explicit and additionally includes argument summaries and 200-character
    memory previews; it should stay on the user's machine unless reviewed.

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
    full_tool_calls = [_deserialize_tool_call(c) for c in calls]
    if mode == "sanitized":
        safe_keys = {
            "id",
            "session_id",
            "tool_name",
            "result_count",
            "duration_ms",
            "called_at",
            "tool_schema_version",
            "memory_ids_touched",
        }
        tool_calls = [
            {
                **{key: value for key, value in call.items() if key in safe_keys},
                "failed": bool(call.get("error")),
            }
            for call in full_tool_calls
        ]
    else:
        tool_calls = full_tool_calls

    # Collect all unique memory ids touched across the calls and resolve
    # them to previews in one batch. Deleted memories are silently dropped
    # (the consumer can spot them by id mismatch).
    touched: set[str] = set()
    for c in full_tool_calls:
        ids = c.get("memory_ids_touched")
        if isinstance(ids, list):
            touched.update(i for i in ids if isinstance(i, str) and i)

    memories: list[dict[str, Any]] = []
    if touched and mode == "local-diagnostic":
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
        "mode": mode,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "prompts_included": False,
            "responses_included": False,
            "tool_argument_summaries_included": mode == "local-diagnostic",
            "memory_previews_included": mode == "local-diagnostic",
            "sharing_guidance": (
                "Metadata-only export; still review project names and identifiers before sharing."
                if mode == "sanitized"
                else "Local diagnostic export; review all fields before sharing."
            ),
        },
        "session": session,
        "tool_calls": tool_calls,
        "memories": memories,
    }

    short_id = session_id.split("-", 1)[0]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"potluck-trace-{mode}-{short_id}-{ts}.json"

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
