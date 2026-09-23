"""Fail-open bridge from coding-agent hooks to Potluck's local trace store.

Hook payloads may contain prompts, commands, patches, and tool results. This
module normalizes an explicit metadata allowlist in memory and writes only that
safe contract to SQLite. Raw provider payloads are never persisted or logged.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from potluck_local.services.memory_db import get_memory_db_path, init_memory_db

Provider = Literal["codex", "claude"]
Attribution = Literal["exact", "inferred", "unknown"]

_ID_NAMESPACE = uuid.UUID("f2c73d9f-480c-48ae-837e-20968e75343f")
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)

_EVENT_MAP: dict[str, tuple[str, str]] = {
    "SessionStart": ("run.started", "started"),
    "SessionEnd": ("run.ended", "completed"),
    "UserPromptSubmit": ("turn.started", "started"),
    "Stop": ("turn.ended", "completed"),
    "PreToolUse": ("tool.started", "started"),
    "PostToolUse": ("tool.completed", "completed"),
    "PostToolUseFailure": ("tool.failed", "failed"),
    "PermissionRequest": ("approval.requested", "waiting"),
    "PermissionDenied": ("approval.denied", "failed"),
    "PreCompact": ("context.compacting", "started"),
    "PostCompact": ("context.compacted", "completed"),
    "SubagentStart": ("agent.started", "started"),
    "SubagentStop": ("agent.ended", "completed"),
    "Interrupt": ("run.interrupted", "stopped"),
    "StopFailure": ("turn.failed", "failed"),
    "Notification": ("notification", "completed"),
    "TaskCreated": ("task.changed", "started"),
    "TaskCompleted": ("task.changed", "completed"),
    "PreModelSwitch": ("model.changing", "started"),
    "PostModelSwitch": ("model.changed", "completed"),
    # Kept for traces produced by older Claude Code builds and configs.
    "ModelSwitch": ("model.changed", "completed"),
}

_METADATA_KEYS = (
    "permission_mode",
    "source",
    "reason",
    "trigger",
    "agent_type",
    "notification_type",
    "from_model",
    "to_model",
)


@dataclass(frozen=True)
class ResourceRef:
    kind: Literal["file", "memory", "task"]
    ref: str
    action: str


@dataclass(frozen=True)
class NormalizedEvent:
    provider: Provider
    provider_session_id: str
    provider_event_name: str
    event_type: str
    status: str
    project: str | None
    model: str | None
    tool_name: str | None
    turn_id: str | None
    correlation_id: str | None
    provider_agent_id: str | None
    agent_kind: str
    agent_label: str
    attribution_confidence: Attribution
    occurred_at: str
    metadata: dict[str, str] = field(default_factory=dict)
    resources: tuple[ResourceRef, ...] = ()


def _short_text(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _project_name(cwd: Any) -> str | None:
    value = _short_text(cwd, 1000)
    if value is None:
        return None
    return Path(value).name[:200] or None


def _repo_relative_path(value: Any, cwd: Any) -> str | None:
    raw_path = _short_text(value, 2000)
    root_value = _short_text(cwd, 2000)
    if raw_path is None or root_value is None:
        return None
    try:
        root = Path(root_value).resolve()
        path = Path(raw_path)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    text = relative.as_posix()
    return text[:500] if text and text != "." else None


def _tool_category(tool_name: str | None) -> str | None:
    if tool_name is None:
        return None
    name = tool_name.lower()
    if "memory_" in name:
        return "memory"
    if "task_" in name:
        return "task"
    if name in {"read", "write", "edit", "notebookedit", "apply_patch"}:
        return "file"
    if name in {"bash", "shell", "exec_command"}:
        return "shell"
    if "search" in name or name in {"glob", "grep"}:
        return "search"
    if "web" in name or "browser" in name:
        return "web"
    return "tool"


def _extract_resources(raw: dict[str, Any], tool_name: str | None) -> tuple[ResourceRef, ...]:
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict) or tool_name is None:
        return ()

    resources: set[tuple[str, str, str]] = set()
    lower_name = tool_name.lower()
    cwd = raw.get("cwd")
    file_action = "read" if lower_name in {"read", "glob", "grep"} else "write"

    for key in ("file_path", "path", "notebook_path"):
        relative = _repo_relative_path(tool_input.get(key), cwd)
        if relative is not None:
            resources.add(("file", relative, file_action))

    if lower_name == "apply_patch":
        patch = tool_input.get("patch") or tool_input.get("input")
        if isinstance(patch, str):
            for match in _PATCH_PATH.finditer(patch):
                relative = _repo_relative_path(match.group(1), cwd)
                if relative is not None:
                    resources.add(("file", relative, "write"))

    if "memory_" in lower_name:
        candidates: list[Any] = [tool_input.get("memory_id")]
        ids = tool_input.get("ids")
        if isinstance(ids, list):
            candidates.extend(ids)
        action = "read" if any(part in lower_name for part in ("search", "list", "get")) else "write"
        for candidate in candidates:
            memory_id = _short_text(candidate, 200)
            if memory_id is not None:
                resources.add(("memory", memory_id, action))

    if "task_" in lower_name:
        task_id = _short_text(tool_input.get("task_id"), 200)
        if task_id is not None:
            action = "read" if any(part in lower_name for part in ("list", "get")) else "write"
            resources.add(("task", task_id, action))

    return tuple(ResourceRef(kind=kind, ref=ref, action=action) for kind, ref, action in sorted(resources))


def normalize_hook_event(
    provider: Provider,
    raw: dict[str, Any],
    *,
    now: str | None = None,
) -> NormalizedEvent | None:
    """Convert one provider payload to the metadata-only Potluck contract."""
    session_id = _short_text(raw.get("session_id"), 500)
    event_name = _short_text(raw.get("hook_event_name"), 100)
    if session_id is None or event_name is None or event_name not in _EVENT_MAP:
        return None

    event_type, status = _EVENT_MAP[event_name]
    source = _short_text(raw.get("source"), 100)
    if event_name == "SessionStart" and source == "resume":
        event_type = "run.resumed"
    elif event_name == "SessionStart" and source == "compact":
        event_type = "context.compacted"

    tool_name = _short_text(raw.get("tool_name"), 200)
    turn_id = _short_text(raw.get("turn_id") or raw.get("prompt_id"), 500)
    agent_id = _short_text(raw.get("agent_id"), 500)
    correlation_id = _short_text(
        raw.get("tool_use_id") or raw.get("task_id") or agent_id or turn_id,
        500,
    )

    if event_name in {"SubagentStart", "SubagentStop"}:
        attribution: Attribution = "exact"
        agent_kind = "subagent"
        agent_label = _short_text(raw.get("agent_type"), 100) or "Subagent"
    elif event_name.startswith("PreTool") or event_name.startswith("PostTool"):
        if agent_id is not None:
            attribution = "exact"
            agent_kind = "subagent"
            agent_label = _short_text(raw.get("agent_type"), 100) or "Subagent"
        elif provider == "codex":
            attribution = "unknown"
            agent_kind = "shared"
            agent_label = "Shared activity"
        else:
            attribution = "exact"
            agent_kind = "root"
            agent_label = "Root agent"
    else:
        attribution = "exact"
        agent_kind = "root"
        agent_label = "Root agent"

    metadata = {
        key: value
        for key in _METADATA_KEYS
        if (value := _short_text(raw.get(key), 100)) is not None
    }
    category = _tool_category(tool_name)
    if category is not None:
        metadata["tool_category"] = category

    timestamp = now or datetime.now(timezone.utc).isoformat()
    return NormalizedEvent(
        provider=provider,
        provider_session_id=session_id,
        provider_event_name=event_name,
        event_type=event_type,
        status=status,
        project=_project_name(raw.get("cwd")),
        model=_short_text(raw.get("model") or raw.get("to_model"), 200),
        tool_name=tool_name,
        turn_id=turn_id,
        correlation_id=correlation_id,
        provider_agent_id=agent_id,
        agent_kind=agent_kind,
        agent_label=agent_label,
        attribution_confidence=attribution,
        occurred_at=timestamp,
        metadata=metadata,
        resources=_extract_resources(raw, tool_name),
    )


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, ":".join(parts)))


def run_id_for(provider: Provider, provider_session_id: str) -> str:
    """Return the stable Potluck run id for a provider session."""
    return _stable_id("run", provider, provider_session_id)


def _event_id(event: NormalizedEvent) -> str:
    identity = event.correlation_id or event.metadata.get("source") or event.metadata.get("reason")
    if identity is None:
        return str(uuid.uuid4())
    return _stable_id(
        event.provider,
        event.provider_session_id,
        event.provider_event_name,
        identity,
        event.turn_id or "",
    )


def _duration_ms(conn: sqlite3.Connection, event: NormalizedEvent) -> int | None:
    if event.event_type not in {"tool.completed", "tool.failed"} or event.correlation_id is None:
        return None
    row = conn.execute(
        """
        SELECT occurred_at FROM agent_events
        WHERE run_id = ? AND correlation_id = ? AND event_type = 'tool.started'
        ORDER BY seq DESC LIMIT 1
        """,
        (
            _stable_id("run", event.provider, event.provider_session_id),
            event.correlation_id,
        ),
    ).fetchone()
    if row is None:
        return None
    try:
        started = datetime.fromisoformat(row[0])
        completed = datetime.fromisoformat(event.occurred_at)
        return max(0, round((completed - started).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


def write_event(
    event: NormalizedEvent,
    db_path: Path | None = None,
    *,
    capture_profile: str = "metadata-only",
    capture_note: str | None = None,
    control_mode: str = "observed",
    task_id: str | None = None,
    cwd: str | None = None,
) -> bool:
    """Write a normalized event. Missing schema or DB contention fails open."""
    path = db_path or get_memory_db_path()
    if not path.exists():
        return False

    run_id = run_id_for(event.provider, event.provider_session_id)
    root_id = _stable_id("agent", run_id, "root")
    received_at = datetime.now(timezone.utc).isoformat()
    if capture_note is None:
        capture_note = (
            "Partial hook coverage; hosted tools and some subagent tool attribution are unavailable."
            if event.provider == "codex"
            else "Hook coverage includes subagent attribution when Claude supplies agent_id."
        )

    try:
        with sqlite3.connect(path, timeout=0.25) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=250")
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'"
            ).fetchone() is None:
                return False

            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, provider, provider_session_id, project, model, status,
                    capture_profile, capture_note, control_mode, task_id, cwd,
                    started_at, last_event_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_session_id) DO UPDATE SET
                    project = COALESCE(excluded.project, agent_runs.project),
                    model = COALESCE(excluded.model, agent_runs.model),
                    capture_profile = CASE
                        WHEN excluded.control_mode = 'managed' THEN excluded.capture_profile
                        ELSE agent_runs.capture_profile
                    END,
                    capture_note = CASE
                        WHEN excluded.control_mode = 'managed' THEN excluded.capture_note
                        ELSE agent_runs.capture_note
                    END,
                    control_mode = CASE
                        WHEN excluded.control_mode = 'managed' THEN 'managed'
                        ELSE agent_runs.control_mode
                    END,
                    task_id = COALESCE(excluded.task_id, agent_runs.task_id),
                    cwd = COALESCE(excluded.cwd, agent_runs.cwd),
                    last_event_at = excluded.last_event_at
                """,
                (
                    run_id,
                    event.provider,
                    event.provider_session_id,
                    event.project,
                    event.model,
                    capture_profile,
                    capture_note,
                    control_mode,
                    task_id,
                    cwd,
                    event.occurred_at,
                    event.occurred_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_instances (
                    id, run_id, provider_agent_id, kind, label, model, status, started_at
                ) VALUES (?, ?, 'root', 'root', 'Root agent', ?, 'active', ?)
                ON CONFLICT(run_id, provider_agent_id) DO UPDATE SET
                    model = COALESCE(excluded.model, agent_instances.model)
                """,
                (root_id, run_id, event.model, event.occurred_at),
            )

            instance_id: str | None = root_id
            if event.agent_kind == "shared":
                instance_id = None
            elif event.agent_kind == "subagent" and event.provider_agent_id is not None:
                instance_id = _stable_id("agent", run_id, event.provider_agent_id)
                conn.execute(
                    """
                    INSERT INTO agent_instances (
                        id, run_id, provider_agent_id, parent_instance_id, kind,
                        label, model, status, started_at
                    ) VALUES (?, ?, ?, ?, 'subagent', ?, ?, 'active', ?)
                    ON CONFLICT(run_id, provider_agent_id) DO UPDATE SET
                        label = excluded.label,
                        model = COALESCE(excluded.model, agent_instances.model)
                    """,
                    (
                        instance_id,
                        run_id,
                        event.provider_agent_id,
                        root_id,
                        event.agent_label,
                        event.model,
                        event.occurred_at,
                    ),
                )

            duration_ms = _duration_ms(conn, event)
            event_id = _event_id(event)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_events (
                    id, run_id, agent_instance_id, event_type, provider_event_name,
                    correlation_id, turn_id, tool_name, status, occurred_at,
                    received_at, duration_ms, attribution_confidence, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    instance_id,
                    event.event_type,
                    event.provider_event_name,
                    event.correlation_id,
                    event.turn_id,
                    event.tool_name,
                    event.status,
                    event.occurred_at,
                    received_at,
                    duration_ms,
                    event.attribution_confidence,
                    json.dumps(event.metadata, separators=(",", ":"), sort_keys=True),
                ),
            )
            if cursor.rowcount > 0:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO agent_resources
                        (event_id, kind, resource_ref, action)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(event_id, item.kind, item.ref, item.action) for item in event.resources],
                )

            if event.event_type == "run.ended":
                conn.execute(
                    "UPDATE agent_runs SET status = 'completed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, run_id),
                )
                conn.execute(
                    "UPDATE agent_instances SET status = 'completed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, root_id),
                )
            elif event.event_type in {"run.started", "run.resumed"}:
                conn.execute(
                    "UPDATE agent_runs SET status = 'active', ended_at = NULL WHERE id = ?",
                    (run_id,),
                )
                conn.execute(
                    "UPDATE agent_instances SET status = 'active', ended_at = NULL WHERE id = ?",
                    (root_id,),
                )
            elif event.event_type == "run.interrupted":
                conn.execute(
                    "UPDATE agent_runs SET status = 'interrupted', ended_at = ? WHERE id = ?",
                    (event.occurred_at, run_id),
                )
                conn.execute(
                    "UPDATE agent_instances SET status = 'completed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, root_id),
                )
            elif event.event_type == "run.failed":
                conn.execute(
                    "UPDATE agent_runs SET status = 'failed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, run_id),
                )
                conn.execute(
                    "UPDATE agent_instances SET status = 'failed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, root_id),
                )
            elif event.event_type == "agent.ended" and instance_id is not None:
                conn.execute(
                    "UPDATE agent_instances SET status = 'completed', ended_at = ? WHERE id = ?",
                    (event.occurred_at, instance_id),
                )

            conn.commit()
        return cursor.rowcount > 0
    except (OSError, sqlite3.Error):
        return False


def ingest_payload(provider: Provider, raw: dict[str, Any], db_path: Path | None = None) -> bool:
    event = normalize_hook_event(provider, raw)
    return write_event(event, db_path) if event is not None else False


def _schema_ready(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runs'"
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("provider", choices=("codex", "claude"))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        db_path = get_memory_db_path()
        if not _schema_ready(db_path):
            import asyncio

            asyncio.run(init_memory_db(db_path))
        raw = json.load(sys.stdin)
        if isinstance(raw, dict):
            ingest_payload(args.provider, raw, db_path)
    except Exception:
        # Observability must never block or alter the agent's work.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
