"""Task-state router.

CRUD endpoints for agent task state.
Mounted at /tasks by app.py.

No privacy gate in v0 - task state is workflow metadata, not user facts.

Project scoping:
- Pass project= to scope reads/writes to a named project.
- Scoped retrieval returns rows where (project = X OR project IS NULL).
- No project = globals only (project IS NULL).
- include_all=true bypasses project filter (browse mode).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

from potluck_local.services.task_state import TaskStatus, get_task_manager

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    task_name: str
    objective: str
    known_constraints: list[str] | None = None
    pending_steps: list[str] | None = None
    metadata: dict[str, Any] | None = None
    conversation_id: str | None = None
    project: str | None = None

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class CompleteStepRequest(BaseModel):
    step: str


class AddErrorRequest(BaseModel):
    error: str
    resolution: str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_tasks(
    status: TaskStatus | None = Query(default=None),
    project: str | None = Query(default=None),
    include_all: bool = Query(default=False),
) -> list[dict[str, Any]]:
    """List all tasks, optionally filtered by status and project.

    project: when given, returns scoped + global tasks.
             when absent, returns globals only.
    include_all: bypasses project filter (browse mode).
    """
    tasks = await get_task_manager().list_tasks(
        status=status,
        project=project or None,
        include_all=include_all,
    )
    logger.info(
        "task_list status=%s project=%s include_all=%s count=%d",
        status,
        project,
        include_all,
        len(tasks),
    )
    return [t.model_dump() for t in tasks]


@router.post("", status_code=201)
async def create_task(req: CreateTaskRequest) -> dict[str, Any]:
    """Create a new task in active status.

    Pass project= to scope the task to a named project.
    Omit project (or pass null) for a global task that applies across all projects.
    """
    task = await get_task_manager().create_task(
        task_name=req.task_name,
        objective=req.objective,
        known_constraints=req.known_constraints,
        pending_steps=req.pending_steps,
        metadata=req.metadata,
        conversation_id=req.conversation_id,
        project=req.project,
    )
    logger.info("task_created id=%s name=%s project=%s", task.id, task.task_name, task.project)
    return task.model_dump()


@router.get("/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    """Return a single task by ID."""
    task = await get_task_manager().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task.model_dump()


@router.patch("/{task_id}")
async def update_task(task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Partially update a task. 400 on unknown keys.

    patch may include 'project' to change or clear (null) the project scope.
    """
    try:
        task = await get_task_manager().update_task(task_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    logger.info("task_updated id=%s fields=%s", task_id, list(patch.keys()))
    return task.model_dump()


@router.post("/{task_id}/complete-step")
async def complete_step(task_id: str, req: CompleteStepRequest) -> dict[str, Any]:
    """Mark a step as completed."""
    task = await get_task_manager().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    task = await get_task_manager().complete_step(task_id, req.step)
    logger.info("task_step_completed id=%s step=%r", task_id, req.step)
    return task.model_dump()


@router.post("/{task_id}/add-error")
async def add_error(task_id: str, req: AddErrorRequest) -> dict[str, Any]:
    """Append an error record to a task."""
    task = await get_task_manager().get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    task = await get_task_manager().add_error(
        task_id, req.error, resolution=req.resolution
    )
    logger.info("task_error_added id=%s", task_id)
    return task.model_dump()


@router.delete("/{task_id}", response_class=Response, status_code=204)
async def delete_task(task_id: str) -> Response:
    """Delete a task. 204 on success, 404 if not found."""
    deleted = await get_task_manager().delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found.")

    logger.info("task_deleted id=%s", task_id)
    return Response(status_code=204)
