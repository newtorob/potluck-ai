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

from potluck_local.services.memory_clustering import ClusterResponse, compute_clusters
from potluck_local.services.memory_db import list_projects, wipe_memory_tables
from potluck_local.services.memory_extraction import (
    Memory,
    MemoryCategory,
    get_memory_extractor,
)
from potluck_local.services.memory_retrieval import (
    KeywordFrequency,
    get_keywords,
    get_memory_retriever,
    get_related,
    semantic_search,
)
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
    semantic: bool | None = None  # None = auto-detect based on query length

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class RelatedMemory(BaseModel):
    memory: Memory
    similarity: float


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


class PinRequest(BaseModel):
    pinned: bool = True


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
    since: str | None = Query(
        default=None,
        description=(
            "ISO-8601 timestamp. When given, returns only rows with updated_at > since. "
            "Pushed to SQL. Combine with project/category/include_all as normal. "
            "Example: 2026-05-27T00:00:00Z"
        ),
    ),
) -> list[dict[str, Any]]:
    """List stored memories.

    project: when given, returns scoped + global memories.
             when absent, returns globals only.
    include_all: bypasses project filter (browse mode).
    since: ISO-8601 string; restricts to rows updated after that timestamp.

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
        since=since,
    )
    logger.info(
        "memory_list category=%s project=%s include_all=%s since=%s limit=%d offset=%d count=%d",
        category,
        project,
        include_all,
        since,
        limit,
        offset,
        len(memories),
    )
    return [m.model_dump() for m in memories]


_EXPORT_PAGE_SIZE = 1_000  # hard cap per page — keeps response payloads bounded


@router.get("/export")
async def export_memories(
    cursor: int = Query(default=0, ge=0, description="Offset into the memory set (for pagination)."),
    since: str | None = Query(
        default=None,
        description=(
            "ISO-8601 timestamp. When given, exports only rows with updated_at > since "
            "(incremental backup). Combine with cursor for large incremental pages. "
            "Example: 2026-05-27T00:00:00Z"
        ),
    ),
) -> dict[str, Any]:
    """Return a page of the active memory set as JSON (excludes soft-deleted).

    The cap per response is 1,000 rows.  Callers that need the full set should
    page by incrementing cursor by 1,000 until has_more is false.

    since: when given, returns only rows updated after that timestamp — enables
    incremental backup instead of full-dump on every call.

    Rationale: the previous 10,000-row cap could produce multi-MB responses in
    a single round-trip.  At 1,000 rows the worst-case payload is ~500 KB which
    is acceptable for a loopback-only export endpoint.  No rate limit is applied
    because this endpoint is loopback-gated by peer_access_middleware.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    retriever = get_memory_retriever()
    memories = await retriever.list_memories(
        limit=_EXPORT_PAGE_SIZE + 1,  # fetch one extra to detect has_more
        offset=cursor,
        include_all=True,
        since=since,
    )
    has_more = len(memories) > _EXPORT_PAGE_SIZE
    page = memories[:_EXPORT_PAGE_SIZE]
    logger.info(
        "memory_export cursor=%d since=%s count=%d has_more=%s",
        cursor, since, len(page), has_more,
    )
    return {
        "memories": [m.model_dump() for m in page],
        "cursor": cursor + len(page),
        "has_more": has_more,
    }


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

    When semantic=True (or when query has >2 words and semantic is not set),
    uses cosine-similarity embedding search.  Otherwise uses keyword LIKE matching.

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

    # Determine whether to use semantic search.
    use_semantic = req.semantic
    if use_semantic is None and req.query is not None:
        use_semantic = len(req.query.split()) > 2
    else:
        use_semantic = bool(use_semantic)

    if use_semantic and req.query is not None:
        pairs = await semantic_search(
            query=req.query,
            project=req.project,
            limit=req.max_memories,
            min_confidence=req.min_confidence,
            include_pinned=True,
        )
        if pairs:
            logger.info(
                "memory_search mode=semantic result_count=%d project=%s",
                len(pairs),
                req.project,
            )
            return [m.model_dump() for m, _ in pairs]
        # Fall through to keyword search if semantic returns nothing
        # (e.g. embedder unavailable).
        logger.info("memory_search mode=semantic_fallback_to_keyword project=%s", req.project)

    retriever = get_memory_retriever()
    memories = await retriever.get_relevant_memories(
        current_messages=messages,
        max_memories=req.max_memories,
        min_confidence=req.min_confidence,
        project=req.project,
    )
    logger.info(
        "memory_search mode=keyword result_count=%d project=%s",
        len(memories),
        req.project,
    )
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
    """Delete all memories (and their embeddings/access log) from the database.
    Requires confirm=true. Scoped to the memory tables only — chats, tasks,
    vault state, and agent observability history are untouched (see
    wipe_memory_tables)."""
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="Pass {'confirm': true} to confirm the wipe.",
        )
    await wipe_memory_tables()
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


@router.get("/keywords")
async def memory_keywords(
    limit: int = Query(default=100, ge=1, le=500),
    project: str | None = Query(default=None),
    min_count: int = Query(default=2, ge=1),
) -> list[dict[str, Any]]:
    """Return keyword frequencies computed over active memory content.

    Filters stopwords and tokens shorter than 3 characters.
    Returns top-K by count, including up to 20 representative memory IDs per keyword.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    kws = await get_keywords(
        project=project or None,
        limit=limit,
        min_count=min_count,
    )
    return [k.model_dump() for k in kws]


@router.get("/clusters")
async def memory_clusters(
    project: str | None = Query(default=None),
) -> dict[str, Any]:
    """Return a 2D UMAP projection of all active memories' embeddings.

    Results are cached; cache invalidates on any memory write/delete/update.
    If fewer than 8 memories have embeddings, falls back to PCA (numpy only).
    Returns degraded=true when the embedding model is unavailable.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    resp = await compute_clusters(project=project or None)
    return resp.model_dump()


@router.get("/{memory_id}/related")
async def related_memories(
    memory_id: str,
    limit: int = Query(default=8, ge=1, le=50),
) -> list[dict[str, Any]]:
    """Return the most similar active memories to a given memory.

    Uses the stored embedding for cosine top-K.  Excludes the memory itself
    and soft-deleted rows.  Returns [] with degraded=true payload when the
    embedder is unavailable.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    # Verify the source memory exists.
    memory = await get_memory_retriever().get_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found.")

    pairs = await get_related(memory_id=memory_id, limit=limit)
    return [
        RelatedMemory(memory=mem, similarity=sim).model_dump()
        for mem, sim in pairs
    ]


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
    """Permanently delete a memory (and its embedding). 204 on success, 404 if not found.

    Hard delete: content is removed from disk, not just flagged inactive — the
    privacy contract for a store the user owns.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    deleted = await get_memory_retriever().delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found.")

    logger.info("memory_deleted id=%s", memory_id)
    return Response(status_code=204)


@router.post("/{memory_id}/pin")
async def pin_memory(memory_id: str, req: PinRequest) -> dict[str, Any]:
    """Pin/unpin a memory so retrieval always injects it regardless of query.

    Body: {"pinned": true|false} (default true). 404 if the memory isn't active.
    """
    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    ok = await get_memory_retriever().set_pinned(memory_id, req.pinned)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found.")

    logger.info("memory_pin_set id=%s pinned=%s", memory_id, req.pinned)
    return {"id": memory_id, "pinned": req.pinned}
