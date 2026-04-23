"""Memory router.

CRUD and search endpoints for the persistent memory subsystem.
Mounted at /memory by app.py.

Privacy gate: both privacy.memory_enabled AND memory.enabled must be True
for the system to accept reads/writes.  GET /memory always returns (possibly
empty) so the UI can detect the disabled state; POST /memory/wipe also
bypasses the gate so users can always clear their data.

Project scoping:
- Pass project= to scope reads/writes to a named project.
- Scoped retrieval returns rows where (project = X OR project IS NULL).
- No project = globals only (project IS NULL).
- include_all=true bypasses project filter (browse mode).
- The router does NOT auto-apply settings.active_project — callers pass it
  explicitly. Only the chat router implicitly uses active_project.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

from potluck_local.services.memory_db import list_projects, reset_memory_db
from potluck_local.services.memory_extraction import (
    MemoryCategory,
    get_memory_extractor,
)
from potluck_local.services.memory_retrieval import get_memory_retriever
from potluck_local.services.settings_manager import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

_DISABLED_DETAIL = "Memory is disabled in Privacy settings"


def _memory_active() -> bool:
    """Return True only when both kill-switch and feature flag are on."""
    s = get_settings()
    return s.privacy.memory_enabled and s.memory.enabled


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateMemoryRequest(BaseModel):
    content: str
    category: MemoryCategory
    conversation_id: str | None = None
    confidence: float = 1.0
    project: str | None = None

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class UpdateMemoryRequest(BaseModel):
    content: str | None = None
    category: MemoryCategory | None = None
    confidence: float | None = None
    project: str | None = None
    _project_set: bool = False  # sentinel — see validator

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]

    model_config = {"extra": "ignore"}


class SearchMemoryRequest(BaseModel):
    query: str | None = None
    current_messages: list[dict[str, Any]] | None = None
    max_memories: int = 20
    min_confidence: float = 0.5
    project: str | None = None

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class ExtractMemoryRequest(BaseModel):
    messages: list[dict[str, Any]]
    conversation_id: str
    project: str | None = None

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class WipeRequest(BaseModel):
    confirm: bool = False


class ImportRequest(BaseModel):
    memories: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_memory_projects() -> list[dict[str, Any]]:
    """Return distinct non-null project names with memory and task counts.

    Used by the frontend project-switcher dropdown.
    Example: [{"project": "potluck", "memory_count": 12, "task_count": 3}]
    """
    return await list_projects()


@router.get("")
async def list_memories(
    category: MemoryCategory | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    project: str | None = Query(default=None),
    include_all: bool = Query(default=False),
) -> list[dict[str, Any]]:
    """List stored memories.

    project: when given, returns scoped + global memories.
             when absent, returns globals only.
    include_all: bypasses project filter (browse mode).

    Always returns even when memory is disabled (returns empty list so the UI
    can display a 'memory is off' state without a separate API call).
    """
    if not _memory_active():
        return []

    retriever = get_memory_retriever()
    memories = await retriever.list_memories(
        category=category,
        limit=limit,
        offset=offset,
        project=project or None,
        include_all=include_all,
    )
    logger.info(
        "memory_list category=%s project=%s include_all=%s limit=%d offset=%d count=%d",
        category,
        project,
        include_all,
        limit,
        offset,
        len(memories),
    )
    return [m.model_dump() for m in memories]


@router.get("/export")
async def export_memories() -> list[dict[str, Any]]:
    """Return the full active memory set as JSON (excludes soft-deleted)."""
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    retriever = get_memory_retriever()
    memories = await retriever.list_memories(limit=10_000, offset=0, include_all=True)
    logger.info("memory_export count=%d", len(memories))
    return [m.model_dump() for m in memories]


@router.post("", status_code=201)
async def create_memory(req: CreateMemoryRequest) -> dict[str, Any]:
    """Explicitly store a single memory.

    Pass project= to scope the memory to a named project.
    Omit project (or pass null) for a global memory that applies across all projects.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    memory = await get_memory_extractor().extract_explicit(
        content=req.content,
        category=req.category,
        conversation_id=req.conversation_id,
        confidence=req.confidence,
        project=req.project,
    )
    logger.info(
        "memory_created id=%s category=%s confidence=%.2f project=%s",
        memory.id,
        memory.category,
        memory.confidence,
        memory.project,
    )
    return memory.model_dump()


@router.post("/search")
async def search_memories(req: SearchMemoryRequest) -> list[dict[str, Any]]:
    """Retrieve memories relevant to a query or message list.

    project: when given, returns scoped + global memories.
             when absent, returns globals only.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    if req.current_messages is not None:
        messages = req.current_messages
    elif req.query is not None:
        messages = [{"role": "user", "content": req.query}]
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of 'query' or 'current_messages'.",
        )

    retriever = get_memory_retriever()
    memories = await retriever.get_relevant_memories(
        current_messages=messages,
        max_memories=req.max_memories,
        min_confidence=req.min_confidence,
        project=req.project,
    )
    logger.info("memory_search result_count=%d project=%s", len(memories), req.project)
    return [m.model_dump() for m in memories]


@router.post("/extract")
async def extract_memories(req: ExtractMemoryRequest) -> list[dict[str, Any]]:
    """Batch-extract memories from a completed conversation.

    project: tag extracted memories with this scope.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    memories = await get_memory_extractor().extract_from_conversation(
        messages=req.messages,
        conversation_id=req.conversation_id,
        project=req.project,
    )
    logger.info(
        "memory_extract conversation_id=%s project=%s extracted=%d",
        req.conversation_id,
        req.project,
        len(memories),
    )
    return [m.model_dump() for m in memories]


@router.post("/wipe")
async def wipe_memories(req: WipeRequest) -> dict[str, str]:
    """Delete all memories from the database. Requires confirm=true."""
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass {'confirm': true} to confirm the wipe.",
        )
    await reset_memory_db()
    logger.info("memory_wiped")
    return {"status": "wiped"}


@router.post("/import")
async def import_memories(req: ImportRequest) -> dict[str, int]:
    """Import a list of memories, skipping IDs that already exist."""
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    retriever = get_memory_retriever()
    imported = 0
    skipped = 0

    for raw in req.memories:
        memory_id = raw.get("id")
        if memory_id and await retriever.get_memory(memory_id) is not None:
            skipped += 1
            continue

        # Re-insert via explicit extraction (strips unknown fields via Pydantic).
        try:
            await get_memory_extractor().extract_explicit(
                content=raw["content"],
                category=raw["category"],
                conversation_id=raw.get("source_conversation_id"),
                confidence=float(raw.get("confidence", 0.7)),
                project=raw.get("project"),
            )
            imported += 1
        except Exception as exc:
            logger.warning("memory_import_skip reason=%s", type(exc).__name__)
            skipped += 1

    logger.info("memory_import imported=%d skipped=%d", imported, skipped)
    return {"imported": imported, "skipped": skipped}


@router.get("/{memory_id}")
async def get_memory(memory_id: str) -> dict[str, Any]:
    """Return a single memory by ID."""
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    memory = await get_memory_retriever().get_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return memory.model_dump()


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, req: UpdateMemoryRequest) -> dict[str, Any]:
    """Partially update a memory's content, category, confidence, or project.

    Pass project=null explicitly to promote a scoped memory to global.
    Omit project entirely to leave the existing scope unchanged.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    # Determine which fields were explicitly provided in the request body.
    raw = req.model_dump(exclude_unset=True)
    if not raw:
        raise HTTPException(status_code=422, detail="No fields to update.")

    # Build kwargs for update_memory, using sentinel for project when absent.
    kwargs: dict[str, Any] = {}
    if "content" in raw:
        kwargs["content"] = raw["content"]
    if "category" in raw:
        kwargs["category"] = raw["category"]
    if "confidence" in raw:
        kwargs["confidence"] = raw["confidence"]

    # Pass project only when the caller explicitly included it in the body,
    # so we can distinguish "set to null" from "not mentioned".
    if "project" in raw:
        kwargs["project"] = raw["project"]

    updated = await get_memory_retriever().update_memory(memory_id, **kwargs)
    if updated is None:
        raise HTTPException(status_code=404, detail="Memory not found.")

    logger.info("memory_updated id=%s fields=%s", memory_id, list(raw.keys()))
    return updated.model_dump()


@router.delete("/{memory_id}", response_class=Response, status_code=204)
async def delete_memory(memory_id: str) -> Response:
    """Soft-delete a memory. 204 on success, 404 if not found."""
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    deleted = await get_memory_retriever().delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found.")

    logger.info("memory_deleted id=%s", memory_id)
    return Response(status_code=204)
