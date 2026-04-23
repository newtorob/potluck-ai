"""Agent observability — session lifecycle and tool-call logging.

All MCP processes and the FastAPI sidecar write through AgentObserver, which
persists rows to mcp_sessions and mcp_tool_calls in the shared SQLite DB.

Privacy invariant: _summarize_args() is a whitelist function.  Only the keys
listed in _SAFE_ARG_KEYS are retained; all others (including prompt content,
query text, and memory content) are silently dropped.  This is the single
chokepoint — all callers route through it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Privacy gate — whitelist only metadata fields
# ---------------------------------------------------------------------------

# Keys that are safe to record — pure metadata, no user content.
# All other keys are stripped before the row is persisted.
_SAFE_ARG_KEYS: frozenset[str] = frozenset(
    {
        "category",
        "project",
        "limit",
        "offset",
        "max_memories",
        "max_results",
        "min_confidence",
        "confidence",
        "include_all",
        "status",
        "task_id",
        # task_update patch keys that are safe
        "name",
    }
)

# Tools whose arguments are 100% safe (session metadata tools).
_ALL_ARGS_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "session_set_model",
        "session_register_mcps",
    }
)


def _summarize_args(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Return a privacy-safe subset of tool args.

    Whitelist approach: only _SAFE_ARG_KEYS pass through.
    session_set_model and session_register_mcps are wholly safe (no content).
    Returns {} when args is None.
    """
    if not args:
        return {}
    if tool_name in _ALL_ARGS_SAFE_TOOLS:
        return dict(args)
    return {k: v for k, v in args.items() if k in _SAFE_ARG_KEYS}


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# AgentObserver
# ---------------------------------------------------------------------------


class AgentObserver:
    """Singleton service for writing observability rows to SQLite."""

    async def open_session(
        self,
        *,
        client_name: str | None,
        client_version: str | None,
        transport: Literal["stdio", "tcp", "internal"],
        pid: int | None,
        model: str | None = None,
        model_source: str | None = None,
        peer_mcps: list[str] | None = None,
    ) -> str:
        """Insert a new mcp_sessions row. Returns the new session_id (UUID)."""
        session_id = str(uuid.uuid4())
        now = _now_iso()
        peer_mcps_json = json.dumps(peer_mcps or [])
        path = get_memory_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute(
                    """
                    INSERT INTO mcp_sessions
                        (id, client_name, client_version, transport, pid,
                         model, model_source, peer_mcps, started_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        client_name,
                        client_version,
                        transport,
                        pid,
                        model,
                        model_source,
                        peer_mcps_json,
                        now,
                        now,
                    ),
                )
                await db.commit()
        except Exception:
            logger.exception(
                "observer_open_session_failed client=%s transport=%s",
                client_name,
                transport,
            )
        logger.info(
            "observer_session_opened id=%s client=%s transport=%s pid=%s",
            session_id,
            client_name,
            transport,
            pid,
        )
        return session_id

    async def update_session(self, session_id: str, **patch: Any) -> None:
        """Partial-update a session row.

        Accepted patch keys: project, model, model_source, peer_mcps.
        Always refreshes last_seen_at.
        """
        allowed = {"project", "model", "model_source", "peer_mcps"}
        clean = {k: v for k, v in patch.items() if k in allowed}
        if not clean and not patch:
            # Still touch last_seen_at even when no patch keys given
            pass
        now = _now_iso()
        set_clauses: list[str] = ["last_seen_at = ?"]
        values: list[Any] = [now]
        for key, val in clean.items():
            set_clauses.append(f"{key} = ?")
            if key == "peer_mcps" and isinstance(val, list):
                values.append(json.dumps(val))
            else:
                values.append(val)
        values.append(session_id)
        path = get_memory_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute(
                    f"UPDATE mcp_sessions SET {', '.join(set_clauses)} WHERE id = ?",
                    values,
                )
                await db.commit()
        except Exception:
            logger.exception("observer_update_session_failed id=%s", session_id)

    async def heartbeat(self, session_id: str) -> None:
        """Touch last_seen_at for an active session."""
        await self.update_session(session_id)

    async def close_session(self, session_id: str) -> None:
        """Set disconnected_at on a session row."""
        now = _now_iso()
        path = get_memory_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute(
                    "UPDATE mcp_sessions SET disconnected_at = ?, last_seen_at = ? WHERE id = ?",
                    (now, now, session_id),
                )
                await db.commit()
        except Exception:
            logger.exception("observer_close_session_failed id=%s", session_id)
        logger.info("observer_session_closed id=%s", session_id)

    async def log_tool_call(
        self,
        *,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        result_count: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        """Insert an mcp_tool_calls row with privacy-safe args_summary."""
        call_id = str(uuid.uuid4())
        summary = _summarize_args(tool_name, args)
        summary_json = json.dumps(summary) if summary else None
        now = _now_iso()
        path = get_memory_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute(
                    """
                    INSERT INTO mcp_tool_calls
                        (id, session_id, tool_name, args_summary,
                         result_count, duration_ms, error, called_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call_id,
                        session_id,
                        tool_name,
                        summary_json,
                        result_count,
                        duration_ms,
                        error,
                        now,
                    ),
                )
                await db.commit()
        except Exception:
            logger.exception(
                "observer_log_tool_call_failed session=%s tool=%s",
                session_id,
                tool_name,
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_observer: AgentObserver | None = None


def get_observer() -> AgentObserver:
    """Return the module-level AgentObserver singleton."""
    global _observer
    if _observer is None:
        _observer = AgentObserver()
    return _observer
