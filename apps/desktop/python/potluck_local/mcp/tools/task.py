"""MCP task tools — create, get, list, update, complete_step, add_error.

No privacy gate — task state is workflow metadata, not personal facts.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from potluck_local.services.task_state import TaskStatus, get_task_manager

logger = logging.getLogger(__name__)


def register_task_tools(mcp: FastMCP) -> None:
    """Register all six task tools onto the provided FastMCP instance."""

    @mcp.tool(
        name="task_create",
        description=(
            "Create a new agent task with an objective and optional constraints "
            "and pending steps. Returns the created TaskState."
        ),
    )
    async def task_create(
        name: str,
        objective: str,
        constraints: list[str] | None = None,
        pending_steps: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        """Create a new task."""
        try:
            task = await get_task_manager().create_task(
                task_name=name,
                objective=objective,
                known_constraints=constraints,
                pending_steps=pending_steps,
                conversation_id=conversation_id,
            )
        except Exception as exc:
            logger.error("task_create_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to create task: {exc}") from exc

        logger.info("task_created id=%s", task.id)
        return task.model_dump()

    @mcp.tool(
        name="task_get",
        description="Retrieve a single task by its ID. Raises an error if not found.",
    )
    async def task_get(task_id: str) -> dict:
        """Fetch a task by ID."""
        try:
            task = await get_task_manager().get_task(task_id)
        except Exception as exc:
            logger.error("task_get_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to get task: {exc}") from exc

        if task is None:
            raise ToolError(f"Task not found: {task_id!r}")

        return task.model_dump()

    @mcp.tool(
        name="task_list",
        description=(
            "List all tasks. Optional status filter: "
            "'active', 'paused', 'completed', or 'abandoned'."
        ),
    )
    async def task_list(status: TaskStatus | None = None) -> list[dict]:
        """List tasks with optional status filter."""
        try:
            tasks = await get_task_manager().list_tasks(status=status)
        except Exception as exc:
            logger.error("task_list_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to list tasks: {exc}") from exc

        logger.info("task_list count=%d status=%s", len(tasks), status)
        return [t.model_dump() for t in tasks]

    @mcp.tool(
        name="task_update",
        description=(
            "Partially update a task. The patch dict may contain any of: "
            "task_name, status, objective, completed_steps, pending_steps, "
            "known_constraints, context_summary, files_touched, errors_encountered, "
            "metadata, conversation_id."
        ),
    )
    async def task_update(task_id: str, patch: dict) -> dict:
        """Apply a partial update to a task."""
        try:
            task = await get_task_manager().update_task(task_id, patch)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.error("task_update_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to update task: {exc}") from exc

        logger.info("task_updated id=%s fields=%s", task_id, list(patch.keys()))
        return task.model_dump()

    @mcp.tool(
        name="task_complete_step",
        description=(
            "Move a step from the pending_steps list to completed_steps. "
            "No-op if the step is not in pending (step is still appended to completed)."
        ),
    )
    async def task_complete_step(task_id: str, step: str) -> dict:
        """Mark a step as completed."""
        try:
            task = await get_task_manager().complete_step(task_id, step)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.error("task_complete_step_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to complete step: {exc}") from exc

        logger.info("task_step_completed id=%s step=%r", task_id, step)
        return task.model_dump()

    @mcp.tool(
        name="task_add_error",
        description=(
            "Append an error record (with optional resolution note) to a task's "
            "errors_encountered list. Use this to track failures mid-task."
        ),
    )
    async def task_add_error(
        task_id: str,
        error: str,
        resolution: str = "",
    ) -> dict:
        """Append an error to a task."""
        try:
            task = await get_task_manager().add_error(
                task_id, error, resolution=resolution
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.error("task_add_error_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to add error to task: {exc}") from exc

        logger.info("task_error_added id=%s", task_id)
        return task.model_dump()
