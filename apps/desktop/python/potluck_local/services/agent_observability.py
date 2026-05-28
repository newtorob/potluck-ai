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
import random
import uuid
from typing import Any, Literal

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path
from potluck_local.utils.time import now_iso as _now_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Friendly-name generator
# ---------------------------------------------------------------------------

# Small word lists for stable per-session nicknames. Two-syllable friendly
# words only — readable at a glance in card chips, no profanity, no slurs,
# no ambiguous proper nouns. ~50 of each gives 2500 unique combinations,
# enough that collisions across the dozen-or-so sessions a user accumulates
# are vanishingly rare.
_ADJECTIVES = (
    "amber", "ancient", "balmy", "brave", "brisk", "calm", "cedar", "cheerful",
    "clever", "cobalt", "copper", "cozy", "crimson", "dappled", "dusky", "eager",
    "earnest", "ember", "fern", "gentle", "golden", "hazel", "humble", "indigo",
    "jolly", "keen", "lively", "lucid", "marbled", "mellow", "merry", "misty",
    "noble", "olive", "patient", "plucky", "quiet", "rosy", "rustic", "scarlet",
    "silver", "spry", "steady", "stoic", "sunny", "tender", "tidy", "vivid",
    "warm", "willowy",
)
_ANIMALS = (
    "alpaca", "badger", "beaver", "bobcat", "boar", "bunting", "caribou",
    "chickadee", "crane", "cricket", "deer", "dingo", "egret", "elk", "ermine",
    "falcon", "ferret", "finch", "fox", "gecko", "gibbon", "hare", "heron",
    "ibex", "jackal", "kingfisher", "lemur", "lynx", "magpie", "marmot", "mink",
    "moose", "newt", "otter", "owl", "panda", "pika", "puffin", "quail",
    "raccoon", "raven", "robin", "salmon", "shrew", "stoat", "sparrow", "tapir",
    "vole", "weasel", "wolf",
)


def generate_friendly_name() -> str:
    """Generate a single 'adjective-animal' nickname. Uses cryptographically
    weak random — collisions are tolerable (the session_id UUID is the
    actual primary key) so we don't need secrets-quality entropy here."""
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_ANIMALS)}"

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
        # NOTE: "name" was removed — it matched task_create's `name` kwarg which
        # holds the user-supplied task title (user content, not metadata).
        # task_update passes its mutable fields inside a nested `patch` dict,
        # which is never serialized by _summarize_args regardless.
    }
)

# Tools whose arguments are 100% safe (session metadata tools).
_ALL_ARGS_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "session_set_model",
        "session_register_mcps",
    }
)

# Tools where it makes sense to record which memory ids were read or
# written, for the debugger's memory-overlay view. Anything outside this
# set has its memory_ids_touched silently dropped — same defense-in-depth
# pattern as the args_summary whitelist.
_MEMORY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "memory_save",
        "memory_search",
        "memory_delete",
        "memory_list",
        "memory_get",
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
# SQL update dispatch table — pre-verified column literals for update_session.
# Keys are the accepted patch parameter names; values are the exact SQL column
# names to use in SET clauses.  Neither the key nor the value ever comes from
# external input — this module owns both sides of the mapping.
# ---------------------------------------------------------------------------

_ALLOWED_SESSION_UPDATES: dict[str, str] = {
    "project": "project",
    "model": "model",
    "model_source": "model_source",
    "peer_mcps": "peer_mcps",
}

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
        friendly_name = generate_friendly_name()
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
                         model, model_source, peer_mcps, started_at, last_seen_at,
                         friendly_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        friendly_name,
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

        Column literals are sourced from _ALLOWED_SESSION_UPDATES (a pre-verified
        dispatch table), never interpolated from caller-supplied strings directly.
        This means even if the allowlist check were somehow bypassed upstream, the
        column name in the SQL is always a known-safe literal from this module.
        """
        clean = {k: v for k, v in patch.items() if k in _ALLOWED_SESSION_UPDATES}
        now = _now_iso()
        set_clauses: list[str] = ["last_seen_at = ?"]
        values: list[Any] = [now]
        for key, val in clean.items():
            col_literal = _ALLOWED_SESSION_UPDATES[key]
            set_clauses.append(f"{col_literal} = ?")
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

    async def sweep_stale_sessions(self, max_age_seconds: int = 300) -> dict[str, int]:
        """Two-pass cleanup of mcp_sessions:

        1. **Mark stale → disconnected**: any active session (`disconnected_at
           IS NULL`) whose `last_seen_at` is older than `max_age_seconds` gets
           `disconnected_at = last_seen_at`. Catches MCP clients that died
           without sending a clean close (crash, kill, network drop).

        2. **Auto-delete worthless sessions**: any row that has no
           `client_name`, no tool calls, AND is older than 30 minutes gets
           DELETED outright (no need to dirty the AgentsView with a row
           that represents zero work and zero identity). These are
           MCP-handshake-then-dead rows: the client opened the transport,
           never identified itself, never invoked a tool, then went away.

        Returns counts: {'marked_disconnected': n, 'deleted_worthless': n}.
        Idempotent; safe to call every minute.
        """
        from datetime import datetime, timedelta, timezone
        path = get_memory_db_path()
        # SQLite stores timestamps as ISO-8601 strings. Compute the cutoff in
        # Python and do string comparison — works because the format
        # _now_iso() produces (`.isoformat()` on a tz-aware UTC datetime)
        # sorts lexicographically the same as chronologically.
        now = datetime.now(timezone.utc)
        stale_cutoff = (now - timedelta(seconds=max_age_seconds)).isoformat()
        # Worthless-row cutoff is fixed at 30 min; an MCP handshake that
        # didn't even send a client_name within half an hour is dead.
        worthless_cutoff = (now - timedelta(minutes=30)).isoformat()

        marked = 0
        deleted = 0
        try:
            async with aiosqlite.connect(path) as db:
                # Pass 1: mark stale active sessions as disconnected.
                cursor = await db.execute(
                    """
                    UPDATE mcp_sessions
                    SET disconnected_at = last_seen_at
                    WHERE disconnected_at IS NULL
                      AND last_seen_at < ?
                    """,
                    (stale_cutoff,),
                )
                marked = cursor.rowcount or 0

                # Pass 2: delete worthless rows. Conditions:
                #   - no client_name (never identified itself), OR client_name = ''
                #   - no tool_calls associated
                #   - older than 30 minutes (give live sessions a grace window)
                # We delete tool_calls first to satisfy the FK relationship,
                # but for these rows there are none by definition.
                cursor = await db.execute(
                    """
                    DELETE FROM mcp_sessions
                    WHERE (client_name IS NULL OR client_name = '')
                      AND last_seen_at < ?
                      AND id NOT IN (
                        SELECT DISTINCT session_id FROM mcp_tool_calls
                      )
                    """,
                    (worthless_cutoff,),
                )
                deleted = cursor.rowcount or 0

                await db.commit()
                if marked or deleted:
                    logger.info(
                        "observer_sweep marked_disconnected=%d deleted_worthless=%d",
                        marked, deleted,
                    )
                return {"marked_disconnected": marked, "deleted_worthless": deleted}
        except Exception:
            logger.exception("observer_stale_sweep_failed")
            return {"marked_disconnected": 0, "deleted_worthless": 0}

    async def log_tool_call(
        self,
        *,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        result_count: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        memory_ids_touched: list[str] | None = None,
    ) -> None:
        """Insert an mcp_tool_calls row with privacy-safe args_summary.

        memory_ids_touched: optional list of memory ids this call read or
        wrote. Stored JSON-encoded so the debugger can join back to the
        `memories` table and render previews. Pure ids — already
        privacy-safe (we never log content here). Whitelist of memory
        tools is enforced via _MEMORY_TOOL_NAMES — pass None for any
        other tool.
        """
        from potluck_local.services.ai_contracts import MCP_TOOL_SCHEMA_VERSION
        call_id = str(uuid.uuid4())
        summary = _summarize_args(tool_name, args)
        summary_json = json.dumps(summary) if summary else None

        # Only persist memory_ids_touched for the whitelisted memory tools.
        # Defense-in-depth: even if a caller hands them to us for some other
        # tool by mistake, we drop them rather than create a side-channel.
        memory_ids_json: str | None = None
        if memory_ids_touched and tool_name in _MEMORY_TOOL_NAMES:
            # Filter to non-empty strings only; cheap normalisation.
            clean = [m for m in memory_ids_touched if isinstance(m, str) and m]
            if clean:
                memory_ids_json = json.dumps(clean)

        now = _now_iso()
        path = get_memory_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute(
                    """
                    INSERT INTO mcp_tool_calls
                        (id, session_id, tool_name, args_summary,
                         result_count, duration_ms, error, called_at,
                         tool_schema_version, memory_ids_touched)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        MCP_TOOL_SCHEMA_VERSION,
                        memory_ids_json,
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
