"""Shared Pydantic schemas for MCP tool inputs.

Re-exports core domain types from the services layer so that the MCP and HTTP
surfaces always return identical shapes.  Input models defined here mirror the
FastAPI request bodies 1:1.
"""

from __future__ import annotations

# Re-export domain types so callers import from one place
from potluck_local.services.memory_extraction import (
    Memory,
    MemoryCategory,
)
from potluck_local.services.task_state import (
    TaskError,
    TaskState,
    TaskStatus,
)

from pydantic import BaseModel

__all__ = [
    "Memory",
    "MemoryCategory",
    "TaskError",
    "TaskState",
    "TaskStatus",
    # Input models
    "MemorySearchInput",
    "MemorySaveInput",
    "MemoryListInput",
    "MemoryDeleteInput",
    "TaskCreateInput",
    "TaskGetInput",
    "TaskListInput",
    "TaskUpdateInput",
    "TaskCompleteStepInput",
    "TaskAddErrorInput",
]


# ---------------------------------------------------------------------------
# Memory tool inputs
# ---------------------------------------------------------------------------


class MemorySearchInput(BaseModel):
    query: str
    category: str | None = None
    max_results: int = 20
    min_confidence: float = 0.5


class MemorySaveInput(BaseModel):
    content: str
    category: MemoryCategory
    confidence: float = 1.0
    conversation_id: str | None = None


class MemoryListInput(BaseModel):
    category: str | None = None
    limit: int = 100
    offset: int = 0


class MemoryDeleteInput(BaseModel):
    memory_id: str


# ---------------------------------------------------------------------------
# Task tool inputs
# ---------------------------------------------------------------------------


class TaskCreateInput(BaseModel):
    name: str
    objective: str
    constraints: list[str] | None = None
    pending_steps: list[str] | None = None
    conversation_id: str | None = None


class TaskGetInput(BaseModel):
    task_id: str


class TaskListInput(BaseModel):
    status: TaskStatus | None = None


class TaskUpdateInput(BaseModel):
    task_id: str
    patch: dict


class TaskCompleteStepInput(BaseModel):
    task_id: str
    step: str


class TaskAddErrorInput(BaseModel):
    task_id: str
    error: str
    resolution: str = ""
