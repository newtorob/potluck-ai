"""Agent task-state manager — persistent task tracking across context windows.

Task states survive context compression and session restarts, giving agentic
workflows a durable record of progress, pending steps, constraints, and errors.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from pydantic import BaseModel

logger = logging.getLogger(__name__)

TaskStatus = Literal["active", "paused", "completed", "abandoned"]

_UPDATABLE_FIELDS = frozenset({
    "task_name", "status", "objective", "completed_steps", "pending_steps",
    "known_constraints", "context_summary", "files_touched",
    "errors_encountered", "metadata", "conversation_id",
})

_JSON_FIELDS = frozenset({
    "completed_steps", "pending_steps", "known_constraints",
    "files_touched", "errors_encountered", "metadata",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskError(BaseModel):
    error: str
    resolution: str
    timestamp: str


class TaskState(BaseModel):
    id: str
    task_name: str
    status: TaskStatus
    objective: str
    completed_steps: list[str]
    pending_steps: list[str]
    known_constraints: list[str]
    context_summary: str
    files_touched: list[str]
    errors_encountered: list[TaskError]
    metadata: dict
    created_at: str
    updated_at: str
    conversation_id: str | None

    def to_context_string(self) -> str:
        """Format task state for injection into the model context window."""
        lines = [
            f"\n--- Active Task: {self.task_name} ---",
            f"Objective: {self.objective}",
        ]

        if self.known_constraints:
            lines.append("Constraints:")
            lines.extend(f"  - {c}" for c in self.known_constraints)

        if self.completed_steps:
            shown = self.completed_steps[-5:]
            lines.append(f"Completed ({len(self.completed_steps)} steps):")
            lines.extend(f"  [done] {step}" for step in shown)
            if len(self.completed_steps) > 5:
                lines.append(f"  ... and {len(self.completed_steps) - 5} earlier steps")

        if self.pending_steps:
            lines.append("Pending:")
            lines.extend(f"  [ ] {step}" for step in self.pending_steps)

        if self.errors_encountered:
            lines.append("Recent errors:")
            for err in self.errors_encountered[-3:]:
                lines.append(f"  - {err.error}: {err.resolution or 'Unresolved'}")

        if self.context_summary:
            lines.append(f"Context summary: {self.context_summary}")

        if self.files_touched:
            lines.append(f"Files involved: {', '.join(self.files_touched[-10:])}")

        lines.append("--- End Task State ---\n")
        return "\n".join(lines)


def _row_to_task(row: dict) -> TaskState:
    errors_raw = json.loads(row.get("errors_encountered") or "[]")
    errors = [
        TaskError(
            error=e.get("error", ""),
            resolution=e.get("resolution", ""),
            timestamp=e.get("timestamp", ""),
        )
        if isinstance(e, dict) else TaskError(error=str(e), resolution="", timestamp="")
        for e in errors_raw
    ]

    return TaskState(
        id=row["id"],
        task_name=row["task_name"],
        status=row["status"],
        objective=row["objective"],
        completed_steps=json.loads(row.get("completed_steps") or "[]"),
        pending_steps=json.loads(row.get("pending_steps") or "[]"),
        known_constraints=json.loads(row.get("known_constraints") or "[]"),
        context_summary=row.get("context_summary") or "",
        files_touched=json.loads(row.get("files_touched") or "[]"),
        errors_encountered=errors,
        metadata=json.loads(row.get("metadata") or "{}"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        conversation_id=row.get("conversation_id"),
    )


class TaskStateManager:
    """CRUD manager for persistent agent task states."""

    async def create_task(
        self,
        *,
        task_name: str,
        objective: str,
        known_constraints: list[str] | None = None,
        pending_steps: list[str] | None = None,
        metadata: dict | None = None,
        conversation_id: str | None = None,
    ) -> TaskState:
        """Create and persist a new task state."""
        ts = _now_iso()
        task = TaskState(
            id=str(uuid.uuid4()),
            task_name=task_name,
            status="active",
            objective=objective,
            completed_steps=[],
            pending_steps=pending_steps or [],
            known_constraints=known_constraints or [],
            context_summary="",
            files_touched=[],
            errors_encountered=[],
            metadata=metadata or {},
            created_at=ts,
            updated_at=ts,
            conversation_id=conversation_id,
        )

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """INSERT INTO task_states
                   (id, task_name, status, objective, completed_steps, pending_steps,
                    known_constraints, context_summary, files_touched, errors_encountered,
                    metadata, created_at, updated_at, conversation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id, task.task_name, task.status, task.objective,
                    json.dumps(task.completed_steps),
                    json.dumps(task.pending_steps),
                    json.dumps(task.known_constraints),
                    task.context_summary,
                    json.dumps(task.files_touched),
                    json.dumps([e.model_dump() for e in task.errors_encountered]),
                    json.dumps(task.metadata),
                    task.created_at, task.updated_at, task.conversation_id,
                ),
            )
            await db.commit()

        logger.info("task_created id=%s name_len=%d", task.id, len(task.task_name))
        return task

    async def get_task(self, task_id: str) -> TaskState | None:
        """Fetch a task by ID."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM task_states WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()

        return _row_to_task(dict(row)) if row else None

    async def list_tasks(self, status: TaskStatus | None = None) -> list[TaskState]:
        """List all tasks, optionally filtered by status."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            if status is not None:
                cursor = await db.execute(
                    "SELECT * FROM task_states WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM task_states ORDER BY updated_at DESC"
                )
            rows = await cursor.fetchall()

        return [_row_to_task(dict(r)) for r in rows]

    async def update_task(self, task_id: str, patch: dict) -> TaskState:
        """Apply a partial update to a task. Raises ValueError for unknown fields."""
        unknown = set(patch.keys()) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Unknown task fields: {unknown!r}")

        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id!r}")

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        ts = _now_iso()
        merged = task.model_dump()
        for key, val in patch.items():
            merged[key] = val
        merged["updated_at"] = ts

        # Re-serialise errors_encountered if it came in as dicts
        raw_errors = merged.get("errors_encountered", [])
        norm_errors = [
            e.model_dump() if isinstance(e, TaskError) else e
            for e in raw_errors
        ]

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """UPDATE task_states
                   SET task_name = ?, status = ?, objective = ?,
                       completed_steps = ?, pending_steps = ?,
                       known_constraints = ?, context_summary = ?,
                       files_touched = ?, errors_encountered = ?,
                       metadata = ?, updated_at = ?, conversation_id = ?
                   WHERE id = ?""",
                (
                    merged["task_name"], merged["status"], merged["objective"],
                    json.dumps(merged["completed_steps"]),
                    json.dumps(merged["pending_steps"]),
                    json.dumps(merged["known_constraints"]),
                    merged.get("context_summary") or "",
                    json.dumps(merged["files_touched"]),
                    json.dumps(norm_errors),
                    json.dumps(merged["metadata"]),
                    ts, merged.get("conversation_id"),
                    task_id,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def complete_step(self, task_id: str, step: str) -> TaskState:
        """Move a step from pending to completed. No-op if step not in pending."""
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id!r}")

        pending = list(task.pending_steps)
        completed = list(task.completed_steps)

        if step in pending:
            pending.remove(step)
        completed.append(step)

        return await self.update_task(
            task_id,
            {"pending_steps": pending, "completed_steps": completed},
        )

    async def add_error(self, task_id: str, error: str, resolution: str = "") -> TaskState:
        """Append an error record to the task's errors_encountered list."""
        task = await self.get_task(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id!r}")

        new_error = TaskError(error=error, resolution=resolution, timestamp=_now_iso())
        errors = [e.model_dump() for e in task.errors_encountered] + [new_error.model_dump()]

        return await self.update_task(task_id, {"errors_encountered": errors})

    async def delete_task(self, task_id: str) -> bool:
        """Hard-delete a task. Returns True if a row was removed."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "DELETE FROM task_states WHERE id = ?", (task_id,)
            )
            await db.commit()
            affected = cursor.rowcount > 0

        if affected:
            logger.info("task_deleted id=%s", task_id)
        return affected


_task_manager: TaskStateManager | None = None


def get_task_manager() -> TaskStateManager:
    """Return the module-level TaskStateManager singleton."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskStateManager()
    return _task_manager
