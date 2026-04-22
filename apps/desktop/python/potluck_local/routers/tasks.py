"""Task-state router.

CRUD endpoints for agent task state.
Mounted at /tasks by app.py.

No privacy gate in v0 — task state is workflow metadata, not user facts.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

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
) -> list[dict[str, Any]]:
    """List all tasks, optionally filtered by status."""
    tasks = await get_task_manager().list_tasks(status=status)
    logger.info("task_list status=%s count=%d", status, len(tasks))
    return [t.model_dump() for t in tasks]


@router.post("", status_code=201)
async def create_task(req: CreateTaskRequest) -> dict[str, Any]:
    """Create a new task in active status."""
    task = await get_task_manager().create_task(
        task_name=req.task_name,
        objective=req.objective,
        known_constraints=req.known_constraints,
        pending_steps=req.pending_steps,
        metadata=req.metadata,
        conversation_id=req.conversation_id,
    )
    logger.info("task_created id=%s name=%s", task.id, task.task_name)
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
    """Partially update a task. 400 on unknown keys."""
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
