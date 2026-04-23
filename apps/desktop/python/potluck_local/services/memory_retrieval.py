"""Memory retrieval service — keyword-based retrieval with confidence/recency scoring.

Retrieves relevant memories for context injection before each model call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    pass

from potluck_local.services.memory_extraction import Memory, MemoryCategory

logger = logging.getLogger(__name__)

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "ought", "used",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "just",
    "now", "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "his", "her", "its", "this", "that", "what", "which",
    "who", "whom", "these", "those", "am", "but", "if", "or", "because",
    "about", "up", "and", "don", "also",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_enabled() -> bool:
    try:
        from potluck_local.services.settings_manager import get_settings
        settings = get_settings()
        return getattr(settings.privacy, "memory_enabled", True)
    except Exception:
        return True


def _row_to_memory(row: dict) -> Memory:
    return Memory(
        id=row["id"],
        content=row["content"],
        category=row["category"],
        confidence=row["confidence"],
        source_type=row["source_type"],
        source_conversation_id=row.get("source_conversation_id"),
        created_at=row["created_at"],
        last_accessed_at=row["last_accessed_at"],
        access_count=row["access_count"],
        is_active=bool(row["is_active"]),
        superseded_by=row.get("superseded_by"),
        project=row.get("project"),
    )


def _extract_keywords(messages: list[dict], max_keywords: int = 15) -> list[str]:
    """Extract meaningful keywords from the last 3 user messages."""
    recent = [m for m in messages[-3:] if m.get("role") == "user"]
    text = " ".join(m.get("content", "") for m in recent)

    words = text.lower().split()
    cleaned = [w.strip(".,!?;:'\"()[]{}") for w in words if len(w) > 2]
    filtered = [w for w in cleaned if w and w not in _STOPWORDS]

    seen: set[str] = set()
    unique: list[str] = []
    for kw in filtered:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)

    return unique[:max_keywords]


def _project_clause(project: str | None) -> tuple[str, list]:
    """Return (SQL fragment, params) for project-scoped filtering.

    When project is given: return rows where project = X OR project IS NULL.
    When project is None: return only global rows (project IS NULL).
    """
    if project is not None:
        return "(project = ? OR project IS NULL)", [project]
    return "project IS NULL", []


class MemoryRetriever:
    """Retrieves and formats memories for prompt injection."""

    async def get_relevant_memories(
        self,
        current_messages: list[dict],
        max_memories: int = 20,
        min_confidence: float = 0.5,
        project: str | None = None,
    ) -> list[Memory]:
        """Retrieve memories relevant to the current conversation context.

        project: when given, returns project-scoped + global memories.
        When None, returns only global memories (project IS NULL).
        """
        if not _memory_enabled():
            return []

        # Normalise empty string to None
        project = project or None

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        always_budget = max(1, max_memories // 2)
        keyword_budget = max_memories - always_budget

        proj_clause, proj_params = _project_clause(project)

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")

            # Tier 1: always-relevant high-confidence preferences + instructions
            cursor = await db.execute(
                f"""SELECT * FROM memories
                   WHERE is_active = 1
                     AND confidence >= ?
                     AND category IN ('preference', 'instruction')
                     AND {proj_clause}
                   ORDER BY confidence DESC, last_accessed_at DESC
                   LIMIT ?""",
                [min_confidence] + proj_params + [always_budget],
            )
            always_rows = [dict(r) for r in await cursor.fetchall()]

            # Tier 2: keyword-matched from other categories
            keyword_rows: list[dict] = []
            keywords = _extract_keywords(current_messages)
            if keywords and keyword_budget > 0:
                # Build parameterized OR conditions — no f-string SQL injection risk
                conditions = " OR ".join("content LIKE ?" for _ in keywords)
                like_params = [f"%{kw}%" for kw in keywords]

                # params order: like values, min_confidence, project params, limit
                params: list = like_params + [min_confidence] + proj_params + [keyword_budget]

                cursor = await db.execute(
                    f"""SELECT * FROM memories
                        WHERE is_active = 1
                          AND ({conditions})
                          AND confidence >= ?
                          AND category NOT IN ('preference', 'instruction')
                          AND {proj_clause}
                        ORDER BY confidence DESC, last_accessed_at DESC
                        LIMIT ?""",  # noqa: S608 — conditions built safely above
                    params,
                )
                keyword_rows = [dict(r) for r in await cursor.fetchall()]

            # Merge, deduplicate by id
            seen_ids: set[str] = set()
            combined: list[dict] = []
            for row in always_rows + keyword_rows:
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    combined.append(row)

            combined = combined[:max_memories]

            # Update access metadata for returned memories
            if combined:
                ts = _now_iso()
                for row in combined:
                    await db.execute(
                        """UPDATE memories
                           SET last_accessed_at = ?, access_count = access_count + 1
                           WHERE id = ?""",
                        (ts, row["id"]),
                    )
                await db.commit()

        memories = [_row_to_memory(r) for r in combined]
        logger.info("memory_retrieval_done count=%d project=%s", len(memories), project)
        return memories

    async def list_memories(
        self,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
        project: str | None = None,
        include_all: bool = False,
    ) -> list[Memory]:
        """Browse active memories without updating access timestamps.

        project: scope filter. Ignored when include_all=True.
        include_all: when True, bypasses the project filter entirely (browse mode).
        """
        # Normalise empty string to None
        project = project or None

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            conditions = ["is_active = 1"]
            params: list = []

            if category is not None:
                conditions.append("category = ?")
                params.append(category)

            if not include_all:
                proj_clause, proj_params = _project_clause(project)
                conditions.append(proj_clause)
                params.extend(proj_params)

            where = " AND ".join(conditions)
            params.extend([limit, offset])

            cursor = await db.execute(
                f"""SELECT * FROM memories
                   WHERE {where}
                   ORDER BY confidence DESC, created_at DESC
                   LIMIT ? OFFSET ?""",
                params,
            )
            rows = await cursor.fetchall()

        return [_row_to_memory(dict(r)) for r in rows]

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Fetch a single memory by ID regardless of is_active state."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            )
            row = await cursor.fetchone()

        if row is None:
            return None
        return _row_to_memory(dict(row))

    async def delete_memory(self, memory_id: str) -> bool:
        """Soft-delete a memory. Returns True if a row was affected."""
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "UPDATE memories SET is_active = 0 WHERE id = ? AND is_active = 1",
                (memory_id,),
            )
            await db.commit()
            affected = cursor.rowcount > 0

        if affected:
            logger.info("memory_soft_deleted id=%s", memory_id)
        return affected

    async def update_memory(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        category: MemoryCategory | None = None,
        confidence: float | None = None,
        project: str | None = ...,  # type: ignore[assignment]
    ) -> Memory | None:
        """Partially update an active memory's mutable fields.

        project uses a sentinel default (...) so callers can distinguish between
        "not provided" and "explicitly set to None" (which clears the scope to global).
        """
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        existing = await self.get_memory(memory_id)
        if existing is None or not existing.is_active:
            return None

        new_content = content if content is not None else existing.content
        new_category = category if category is not None else existing.category
        new_confidence = (
            min(max(confidence, 0.0), 1.0) if confidence is not None else existing.confidence
        )
        # If project was not provided (sentinel ...), keep existing value.
        # If explicitly passed (including None), use the new value.
        if project is ...:  # type: ignore[comparison-overlap]
            new_project: str | None = existing.project
        else:
            new_project = project or None  # coerce "" -> None

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """UPDATE memories
                   SET content = ?, category = ?, confidence = ?, project = ?
                   WHERE id = ?""",
                (new_content, new_category, new_confidence, new_project, memory_id),
            )
            await db.commit()

        return await self.get_memory(memory_id)

    def format_memories_for_prompt(self, memories: list[Memory]) -> str:
        """Format memories into a system-prompt section, grouped by category."""
        if not memories:
            return ""

        sections: dict[str, list[str]] = {
            "instruction": [],
            "preference": [],
            "person": [],
            "project": [],
            "fact": [],
            "decision": [],
        }

        for mem in memories:
            cat = mem.category if mem.category in sections else "fact"
            sections[cat].append(mem.content)

        lines = ["\n--- Memory Context (from previous conversations) ---"]

        if sections["instruction"]:
            lines.append("\nStanding instructions from the user:")
            lines.extend(f"- {item}" for item in sections["instruction"])

        if sections["preference"]:
            lines.append("\nUser preferences:")
            lines.extend(f"- {item}" for item in sections["preference"])

        if sections["person"]:
            lines.append("\nPeople the user has mentioned:")
            lines.extend(f"- {item}" for item in sections["person"])

        if sections["project"]:
            lines.append("\nProject context:")
            lines.extend(f"- {item}" for item in sections["project"])

        remaining = sections["fact"] + sections["decision"]
        if remaining:
            lines.append("\nRelevant context:")
            lines.extend(f"- {item}" for item in remaining)

        lines.append("\n--- End Memory Context ---")
        return "\n".join(lines)


_retriever: MemoryRetriever | None = None


def get_memory_retriever() -> MemoryRetriever:
    """Return the module-level MemoryRetriever singleton."""
    global _retriever
    if _retriever is None:
        _retriever = MemoryRetriever()
    return _retriever
