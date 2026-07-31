"""Agent task-state manager — persistent task tracking across context windows.

Task states survive context compression and session restarts, giving agentic
workflows a durable record of progress, pending steps, constraints, and errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from pydantic import BaseModel, field_validator

from potluck_local.services.memory_db import project_clause

logger = logging.getLogger(__name__)

TaskStatus = Literal["active", "paused", "completed", "abandoned"]

_UPDATABLE_FIELDS = frozenset({
    "task_name", "status", "objective", "completed_steps", "pending_steps",
    "known_constraints", "context_summary", "files_touched",
    "errors_encountered", "metadata", "conversation_id", "project",
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
    project: str | None = None  # null = global task, applies across all projects

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        """Coerce empty string to None — both represent 'global'."""
        if v == "":
            return None
        return v  # type: ignore[return-value]

    def to_context_string(self) -> str:
        """Format task state for injection into the model context window."""
        project_prefix = f"Project: {self.project} · " if self.project else ""
        lines = [
            f"\n--- Active Task: {project_prefix}{self.task_name} ---",
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
        project=row.get("project"),
    )


# project_clause(project, alias) is imported from memory_db — shared with
# memory_retrieval + memory_clustering.


class TaskStateManager:
    """CRUD manager for persistent agent task states.

    update_task() previously did an unlocked read -> full-row merge -> write:
    two concurrent callers (e.g. two agents patching the same task) each read
    the row before either wrote, so whichever write landed second silently
    clobbered the first caller's change. Every mutation now serializes
    through a per-task_id asyncio.Lock (see _lock_for) so a read-merge-write
    is atomic with respect to other calls for the same task_id within this
    process — the only place concurrent agents actually contend, since they
    all talk to the same sidecar.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, task_id: str) -> asyncio.Lock:
        """Return the (lazily-created) lock serializing writes to task_id."""
        async with self._locks_guard:
            lock = self._locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[task_id] = lock
            return lock

    async def create_task(
        self,
        *,
        task_name: str,
        objective: str,
        known_constraints: list[str] | None = None,
        pending_steps: list[str] | None = None,
        metadata: dict | None = None,
        conversation_id: str | None = None,
        project: str | None = None,
    ) -> TaskState:
        """Create and persist a new task state.

        project: scope this task to a named project. None = global task,
        applies across all projects.
        """
        # Normalise empty string to None
        project = project or None

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
            project=project,
        )

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """INSERT INTO task_states
                   (id, task_name, status, objective, completed_steps, pending_steps,
                    known_constraints, context_summary, files_touched, errors_encountered,
                    metadata, created_at, updated_at, conversation_id, project)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    task.project,
                ),
            )
            await db.commit()

        logger.info("task_created id=%s name_len=%d project=%s", task.id, len(task.task_name), project)
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

    async def list_tasks(
        self,
        status: TaskStatus | None = None,
        project: str | None = None,
        include_all: bool = False,
    ) -> list[TaskState]:
        """List all tasks, optionally filtered by status and project.

        project: scope filter. Ignored when include_all=True.
        include_all: when True, bypasses the project filter entirely (browse mode).
        """
        # Normalise empty string to None
        project = project or None

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            conditions: list[str] = []
            params: list = []

            if status is not None:
                conditions.append("status = ?")
                params.append(status)

            if not include_all:
                proj_clause, proj_params = project_clause(project)
                conditions.append(proj_clause)
                params.extend(proj_params)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            cursor = await db.execute(
                f"SELECT * FROM task_states {where} ORDER BY updated_at DESC",
                params,
            )
            rows = await cursor.fetchall()

        return [_row_to_task(dict(r)) for r in rows]

    async def update_task(self, task_id: str, patch: dict) -> TaskState:
        """Apply a partial update to a task. Raises ValueError for unknown fields.

        The whole read -> merge -> write happens under this task's lock, so a
        concurrent update_task/complete_step/add_error call for the same
        task_id can't read the pre-patch row and clobber this write (or vice
        versa) — see the class docstring.
        """
        unknown = set(patch.keys()) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Unknown task fields: {unknown!r}")

        lock = await self._lock_for(task_id)
        async with lock:
            task = await self.get_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id!r}")
            return await self._write_patch(task_id, task, patch)

    async def _write_patch(self, task_id: str, task: TaskState, patch: dict) -> TaskState:
        """Merge `patch` onto `task` and persist. Caller must hold the
        per-task_id lock — `task` must be a read taken under that same lock
        so the merge reflects the latest committed state."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        ts = _now_iso()
        merged = task.model_dump()
        for key, val in patch.items():
            merged[key] = val
        merged["updated_at"] = ts

        # Normalise project in the merged dict
        if "project" in merged:
            merged["project"] = merged["project"] or None

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
                       metadata = ?, updated_at = ?, conversation_id = ?,
                       project = ?
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
                    merged.get("project"),
                    task_id,
                ),
            )
            await db.commit()

        updated = await self.get_task(task_id)
        assert updated is not None
        return updated

    async def complete_step(self, task_id: str, step: str) -> TaskState:
        """Move a step from pending to completed. Idempotent: a step that is
        already completed (or was never pending) is a no-op — retrying the
        same completion twice must not append a second "completed" entry."""
        lock = await self._lock_for(task_id)
        async with lock:
            task = await self.get_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id!r}")

            if step not in task.pending_steps:
                # Already completed, or never a tracked pending step: no
                # state to transition, so no write. This is what makes a
                # retried complete-step call idempotent instead of
                # re-appending "step" to completed_steps.
                return task

            pending = [s for s in task.pending_steps if s != step]
            completed = list(task.completed_steps) + [step]
            return await self._write_patch(
                task_id, task,
                {"pending_steps": pending, "completed_steps": completed},
            )

    async def add_error(self, task_id: str, error: str, resolution: str = "") -> TaskState:
        """Append an error record to the task's errors_encountered list."""
        lock = await self._lock_for(task_id)
        async with lock:
            task = await self.get_task(task_id)
            if task is None:
                raise ValueError(f"Task not found: {task_id!r}")

            new_error = TaskError(error=error, resolution=resolution, timestamp=_now_iso())
            errors = [e.model_dump() for e in task.errors_encountered] + [new_error.model_dump()]
            return await self._write_patch(task_id, task, {"errors_encountered": errors})

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
