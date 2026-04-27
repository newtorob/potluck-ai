"""Memory extraction service — LLM-based fact/preference extraction.

Runs the loaded model over a conversation to produce structured Memory objects,
storing them in the SQLite memory DB with Jaccard-based deduplication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

MemoryCategory = Literal["preference", "fact", "decision", "project", "person", "instruction"]
SourceType = Literal["extracted", "explicit", "agent_state"]

_CONFIDENCE_THRESHOLD = 0.7
_JACCARD_THRESHOLD = 0.7

_EXTRACTION_PROMPT = """Analyze this conversation and extract facts, preferences, decisions, or context the user expressed.

Return ONLY a JSON array. Each element must have exactly these keys:
- "content": the memory as a clear, self-contained statement (string)
- "category": one of "preference", "fact", "decision", "project", "person", "instruction" (string)
- "confidence": 0.0-1.0 how confident you are this is worth remembering (number)

Rules:
- Only extract things the USER said or clearly implied
- Make each memory self-contained without the conversation
- Skip greetings, questions, acknowledgments, and ephemeral details
- If nothing worth remembering was said, return []

Example: [{"content": "User prefers Python over JavaScript", "category": "preference", "confidence": 0.9}]

Conversation:
{conversation}

JSON array:"""


class Memory(BaseModel):
    id: str
    content: str
    category: MemoryCategory
    confidence: float
    source_type: SourceType
    source_conversation_id: str | None
    created_at: str
    last_accessed_at: str
    access_count: int
    is_active: bool
    superseded_by: str | None = None
    project: str | None = None  # null = global memory, applies across all projects

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        """Coerce empty string to None — both represent 'global'."""
        if v == "":
            return None
        return v  # type: ignore[return-value]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _parse_json_response(text: str) -> list[dict]:
    """Defensively parse a JSON array from model output.

    Handles markdown fences, missing outer brackets, and trailing prose.
    """
    # Strip markdown fences
    text = re.sub(r"```(?:json)?", "", text).strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        return []
    except json.JSONDecodeError:
        pass

    # Find the first [ ... ] span
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def _memory_enabled() -> bool:
    """Return True if memory extraction is enabled in settings."""
    try:
        from potluck_local.services.settings_manager import get_settings
        settings = get_settings()
        return getattr(settings.privacy, "memory_enabled", True)
    except Exception:
        return True


def _fire_embed(memory_id: str, content: str) -> None:
    """Schedule an embedding generation as a fire-and-forget asyncio task.

    Safe to call from any async context.  If the event loop isn't running
    (shouldn't happen in production) this is a no-op.
    """
    async def _embed_and_save() -> None:
        try:
            from potluck_local.services.embeddings import get_embedder
            from potluck_local.services.memory_db import save_embedding
            from potluck_local.services.memory_clustering import get_cluster_cache

            embedder = get_embedder()
            vec = await embedder.embed(content)
            if vec is not None:
                await save_embedding(memory_id, vec, embedder.MODEL_NAME)
                await get_cluster_cache().invalidate_all()
        except Exception as exc:
            logger.warning("embed_on_insert_failed id=%s reason=%s", memory_id, exc)

    asyncio.create_task(_embed_and_save())


class MemoryExtractor:
    """Extracts and persists memories from conversations via the loaded LLM."""

    async def extract_from_conversation(
        self,
        messages: list[dict],
        conversation_id: str,
        project: str | None = None,
    ) -> list[Memory]:
        """Extract memories from a conversation; returns only newly-inserted ones.

        project: tag each new memory with this scope. None = global memory,
        applies across all projects.
        """
        if not _memory_enabled():
            return []

        # Normalise empty string to None
        project = project or None

        from potluck_local.services.runtime import get_runtime
        runtime = get_runtime()
        if not runtime.is_loaded():
            logger.warning("memory_extraction_skipped reason=no_model_loaded")
            return []

        conversation_text = "\n\n".join(
            f"{m['role'].upper()}: {m.get('content', '')}"
            for m in messages
            if m.get("content")
        )

        prompt = _EXTRACTION_PROMPT.format(conversation=conversation_text)
        try:
            result = await runtime.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning("memory_extraction_llm_error type=%s", type(exc).__name__)
            return []

        raw_text = result["choices"][0]["message"]["content"]
        raw_items = _parse_json_response(raw_text)

        inserted: list[Memory] = []
        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            for item in raw_items:
                if not isinstance(item, dict) or not item.get("content"):
                    continue

                content: str = str(item["content"]).strip()
                category: str = item.get("category", "fact")
                if category not in ("preference", "fact", "decision", "project", "person", "instruction"):
                    category = "fact"
                confidence: float = min(max(float(item.get("confidence", 0.7)), 0.0), 1.0)

                if confidence < _CONFIDENCE_THRESHOLD:
                    continue

                # Deduplicate against existing active memories in same category.
                # Deduplication is intentionally global (across projects) — a near-
                # identical fact in two scopes still gets a confidence bump on the
                # existing row rather than a duplicate insert.
                cursor = await db.execute(
                    "SELECT id, content, confidence FROM memories WHERE category = ? AND is_active = 1",
                    (category,),
                )
                existing_rows = await cursor.fetchall()

                duplicate_id: str | None = None
                for ex_id, ex_content, ex_conf in existing_rows:
                    if _jaccard(content, ex_content) >= _JACCARD_THRESHOLD:
                        duplicate_id = ex_id
                        new_conf = min(ex_conf + 0.1, 1.0)
                        ts = _now_iso()
                        await db.execute(
                            "UPDATE memories SET confidence = ?, last_accessed_at = ? WHERE id = ?",
                            (new_conf, ts, ex_id),
                        )
                        break

                if duplicate_id is None:
                    mem = Memory(
                        id=str(uuid.uuid4()),
                        content=content,
                        category=category,  # type: ignore[arg-type]
                        confidence=confidence,
                        source_type="extracted",
                        source_conversation_id=conversation_id,
                        created_at=_now_iso(),
                        last_accessed_at=_now_iso(),
                        access_count=0,
                        is_active=True,
                        superseded_by=None,
                        project=project,
                    )
                    await db.execute(
                        """INSERT INTO memories
                           (id, content, category, confidence, source_type,
                            source_conversation_id, created_at, last_accessed_at,
                            access_count, is_active, project)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            mem.id, mem.content, mem.category, mem.confidence,
                            mem.source_type, mem.source_conversation_id,
                            mem.created_at, mem.last_accessed_at,
                            mem.access_count, 1, mem.project,
                        ),
                    )
                    inserted.append(mem)

            await db.commit()

        # Fire-and-forget embedding generation for each new memory.
        for mem in inserted:
            _fire_embed(mem.id, mem.content)

        logger.info(
            "memory_extraction_done conversation_id=%s project=%s inserted=%d skipped_dupes=%d",
            conversation_id,
            project,
            len(inserted),
            len(raw_items) - len(inserted),
        )
        return inserted

    async def extract_explicit(
        self,
        content: str,
        category: MemoryCategory,
        conversation_id: str | None = None,
        confidence: float = 1.0,
        project: str | None = None,
    ) -> Memory:
        """Persist an explicit user-stated memory. Always inserts; no deduplication.

        project: scope this memory to a named project. None = global memory,
        applies across all projects.
        """
        # Normalise empty string to None
        project = project or None

        mem = Memory(
            id=str(uuid.uuid4()),
            content=content.strip(),
            category=category,
            confidence=min(max(confidence, 0.0), 1.0),
            source_type="explicit",
            source_conversation_id=conversation_id,
            created_at=_now_iso(),
            last_accessed_at=_now_iso(),
            access_count=0,
            is_active=True,
            superseded_by=None,
            project=project,
        )

        from potluck_local.services.memory_db import get_memory_db_path
        db_path = get_memory_db_path()

        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(
                """INSERT INTO memories
                   (id, content, category, confidence, source_type,
                    source_conversation_id, created_at, last_accessed_at,
                    access_count, is_active, project)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mem.id, mem.content, mem.category, mem.confidence,
                    mem.source_type, mem.source_conversation_id,
                    mem.created_at, mem.last_accessed_at,
                    mem.access_count, 1, mem.project,
                ),
            )
            await db.commit()

        # Fire-and-forget embedding generation.
        _fire_embed(mem.id, mem.content)

        logger.info(
            "memory_explicit_saved id=%s category=%s project=%s",
            mem.id, mem.category, mem.project,
        )
        return mem


_extractor: MemoryExtractor | None = None


def get_memory_extractor() -> MemoryExtractor:
    """Return the module-level MemoryExtractor singleton."""
    global _extractor
    if _extractor is None:
        _extractor = MemoryExtractor()
    return _extractor
