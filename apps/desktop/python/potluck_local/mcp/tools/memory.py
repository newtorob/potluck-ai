"""MCP memory tools — search, save, list, delete.

All four tools honour the same privacy gate as the HTTP router:
both privacy.memory_enabled and memory.enabled must be True.
If the gate is closed, tools return empty results and log a warning rather
than raising errors — an agent calling blindly should degrade gracefully.

Project scoping:
All tools accept an optional `project` parameter. Pass project=None (the
default) for global memory that applies across all projects. Pass a project
name to scope the operation to that project. Retrieval with a project set
returns both project-scoped AND global memories (project IS NULL), so
standing preferences and instructions always surface.
"""

import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from potluck_local.services.memory_extraction import MemoryCategory, get_memory_extractor
from potluck_local.services.memory_retrieval import get_memory_retriever
from potluck_local.services.settings_manager import get_settings

logger = logging.getLogger(__name__)


def _memory_active() -> bool:
    """Return True only when both kill-switch and feature flag are on."""
    try:
        s = get_settings()
        return s.privacy.memory_enabled and s.memory.enabled
    except Exception:
        return True


def register_memory_tools(mcp: FastMCP) -> None:
    """Register all four memory tools onto the provided FastMCP instance."""

    @mcp.tool(
        name="memory_search",
        description=(
            "CALL THIS BEFORE answering any question about the user's project, "
            "preferences, hardware, code conventions, prior decisions, or 'how should I' "
            "questions. Surfaces ranked memories from previous sessions that the user "
            "explicitly saved or that were extracted from prior conversations. "
            "Cost is ~50ms; the result frequently changes the right answer.\n\n"
            "Always run this at session start with a short topic query like "
            "\"current project context\" to load whatever the user has saved. "
            "If you find nothing, that's fine — proceed normally.\n\n"
            "Scope: pass project=None (default) for global-only. Pass a project name "
            "(e.g. 'potluck', 'strake') to combine that project's memories with globals."
        ),
    )
    async def memory_search(
        query: str,
        category: str | None = None,
        max_results: int = 20,
        min_confidence: float = 0.5,
        project: str | None = None,
    ) -> list[dict]:
        """Retrieve memories relevant to query.

        project: scope filter. None = global-only. Named project = scoped + globals.
        project=None means 'global memory, applies across all projects.'
        """
        if not _memory_active():
            logger.warning("memory_search_skipped reason=memory_disabled")
            return []

        messages = [{"role": "user", "content": query}]
        retriever = get_memory_retriever()
        try:
            memories = await retriever.get_relevant_memories(
                current_messages=messages,
                max_memories=max_results,
                min_confidence=min_confidence,
                project=project or None,
            )
        except Exception as exc:
            logger.error("memory_search_error type=%s", type(exc).__name__)
            raise ToolError(f"Memory search failed: {exc}") from exc

        # If a category filter was requested, apply it post-retrieval
        if category is not None:
            memories = [m for m in memories if m.category == category]

        logger.info("memory_search result_count=%d project=%s", len(memories), project)
        return [m.model_dump() for m in memories]

    @mcp.tool(
        name="memory_save",
        description=(
            "CALL THIS WHEN the user states a preference, makes a decision, mentions "
            "a project fact, names a tool/library choice, or provides any context that "
            "should outlive this session. Persists a memory that future sessions will "
            "surface via memory_search.\n\n"
            "Pick category honestly: 'preference' (what they like/dislike), "
            "'fact' (objective info), 'decision' (a choice with reasoning), "
            "'project' (project-specific context), 'person' (someone they mention), "
            "'instruction' (a directive about how to work).\n\n"
            "Save proactively for clearly durable facts. Ask first for anything "
            "ambiguous or that might be temporary. Do not save chitchat or one-off "
            "questions.\n\n"
            "Scope: pass project=None for global memory (applies everywhere) or a "
            "project name (e.g. 'potluck') to scope to that project only."
        ),
    )
    async def memory_save(
        content: str,
        category: MemoryCategory,
        confidence: float = 1.0,
        conversation_id: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Persist an explicit memory.

        project: scope this memory to a project. None = global memory,
        applies across all projects.
        """
        if not _memory_active():
            logger.warning("memory_save_skipped reason=memory_disabled")
            raise ToolError(
                "Memory is disabled in Privacy settings. Enable it to save memories."
            )

        try:
            memory = await get_memory_extractor().extract_explicit(
                content=content,
                category=category,
                conversation_id=conversation_id,
                confidence=confidence,
                project=project or None,
            )
        except Exception as exc:
            logger.error("memory_save_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to save memory: {exc}") from exc

        logger.info("memory_saved id=%s category=%s project=%s", memory.id, memory.category, memory.project)
        return memory.model_dump()

    @mcp.tool(
        name="memory_list",
        description=(
            "Browse all stored memories — use this when the user asks 'what do you "
            "know about X' or wants a full audit of what's saved. Prefer memory_search "
            "for finding specific context relevant to a query; memory_list is for "
            "enumeration.\n\n"
            "Scope: project=None for global-only, a project name for scoped+global, "
            "include_all=True for everything regardless of scope (browse mode)."
        ),
    )
    async def memory_list(
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
        project: str | None = None,
        include_all: bool = False,
    ) -> list[dict]:
        """List active memories.

        project: scope filter. None = global-only. Named project = scoped + globals.
        include_all: bypass project filter entirely (browse mode).
        project=None means 'global memory, applies across all projects.'
        """
        if not _memory_active():
            logger.warning("memory_list_skipped reason=memory_disabled")
            return []

        retriever = get_memory_retriever()
        try:
            memories = await retriever.list_memories(
                category=category,
                limit=limit,
                offset=offset,
                project=project or None,
                include_all=include_all,
            )
        except Exception as exc:
            logger.error("memory_list_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to list memories: {exc}") from exc

        logger.info("memory_list count=%d project=%s", len(memories), project)
        return [m.model_dump() for m in memories]

    @mcp.tool(
        name="memory_delete",
        description=(
            "Soft-delete a memory by its ID. The memory is marked inactive "
            "and will no longer be returned by search or list."
        ),
    )
    async def memory_delete(memory_id: str) -> dict:
        """Soft-delete a memory."""
        if not _memory_active():
            logger.warning("memory_delete_skipped reason=memory_disabled")
            raise ToolError(
                "Memory is disabled in Privacy settings. Enable it to manage memories."
            )

        retriever = get_memory_retriever()
        try:
            deleted = await retriever.delete_memory(memory_id)
        except Exception as exc:
            logger.error("memory_delete_error type=%s", type(exc).__name__)
            raise ToolError(f"Failed to delete memory: {exc}") from exc

        if not deleted:
            raise ToolError(f"Memory not found: {memory_id!r}")

        logger.info("memory_deleted id=%s", memory_id)
        return {"deleted": True}
