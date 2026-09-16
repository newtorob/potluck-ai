"""Vault ingestion — markdown folder → memory chunks.

Walks a configured folder for `.md` files, splits each into heading-bounded
chunks, embeds them, and inserts as memories with source_type='vault' +
category='reference'. Tracked in `vault_files` so re-runs only touch changed
files (SHA256 comparison) and removed files have their chunks reaped.

Privacy: ingested content stays in the local SQLite memory store. It never
leaves the desktop unless the user explicitly exports.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from potluck_local.services.embeddings import get_embedder
from potluck_local.services.memory_db import get_memory_db_path, save_embedding

logger = logging.getLogger(__name__)

# Chunks larger than this get split further on paragraph boundaries to keep
# embedding quality reasonable (the embedder truncates at ~256 tokens).
_MAX_CHUNK_CHARS = 1800
# Chunks smaller than this are merged with the previous chunk — avoids
# degenerate single-line headings becoming their own node.
_MIN_CHUNK_CHARS = 80


@dataclass
class ScanResult:
    files_scanned: int
    files_added: int
    files_updated: int
    files_removed: int
    chunks_inserted: int
    chunks_deleted: int


@dataclass
class _Chunk:
    heading: str
    body: str
    anchor: str  # heading slug used in source_ref


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(heading: str) -> str:
    """Markdown-style anchor slug. Mirrors common convention well enough for
    a stable per-file identifier; we don't need GitHub-exact compatibility."""
    s = heading.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s[:80] or "section"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_markdown(text: str) -> list[_Chunk]:
    """Split markdown into heading-bounded chunks.

    A chunk starts at a `#`-prefixed heading line and runs until the next
    heading. Files with no headings become a single chunk with heading=''.
    Oversize chunks are split further on blank-line paragraph boundaries.
    Tiny trailing chunks are merged into the previous chunk.
    """
    lines = text.splitlines()
    raw_sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_body: list[str] = []

    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            if current_heading or current_body:
                raw_sections.append((current_heading, current_body))
            current_heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            current_body = []
        else:
            current_body.append(line)
    if current_heading or current_body:
        raw_sections.append((current_heading, current_body))

    chunks: list[_Chunk] = []
    seen_anchors: dict[str, int] = {}

    for heading, body_lines in raw_sections:
        body = "\n".join(body_lines).strip()
        if not body and not heading:
            continue

        # Split oversize bodies on paragraph boundaries
        pieces: list[str]
        if len(body) > _MAX_CHUNK_CHARS:
            pieces = []
            buf: list[str] = []
            buf_len = 0
            for para in re.split(r"\n\s*\n", body):
                if buf_len + len(para) > _MAX_CHUNK_CHARS and buf:
                    pieces.append("\n\n".join(buf))
                    buf = [para]
                    buf_len = len(para)
                else:
                    buf.append(para)
                    buf_len += len(para)
            if buf:
                pieces.append("\n\n".join(buf))
        else:
            pieces = [body]

        base_anchor = _slugify(heading) if heading else "intro"
        for idx, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            anchor = base_anchor if len(pieces) == 1 else f"{base_anchor}-{idx + 1}"
            # Disambiguate repeated headings within the same file
            count = seen_anchors.get(anchor, 0)
            seen_anchors[anchor] = count + 1
            if count:
                anchor = f"{anchor}-{count + 1}"

            chunks.append(_Chunk(heading=heading, body=piece, anchor=anchor))

    # Merge tiny tails into their preceding chunk so we don't get noisy
    # one-line nodes.
    merged: list[_Chunk] = []
    for c in chunks:
        if merged and len(c.body) < _MIN_CHUNK_CHARS:
            prev = merged[-1]
            prev.body = f"{prev.body}\n\n{c.body}".strip()
        else:
            merged.append(c)

    return merged


def _chunk_content(chunk: _Chunk, file_path: Path) -> str:
    """Compose the memory-content string for a chunk. Includes the heading
    and the source filename so retrieval surfaces useful context even when
    the embedding alone is borderline."""
    label = chunk.heading or file_path.stem
    return f"# {label}\n\n{chunk.body}\n\n— from {file_path.name}"


async def _delete_chunks_for_file(db: aiosqlite.Connection, file_path: str) -> int:
    """Delete all memories whose source_ref points at this file. Returns count."""
    prefix = f"vault:{file_path}#"
    cursor = await db.execute(
        "SELECT id FROM memories WHERE source_ref LIKE ?",
        (prefix + "%",),
    )
    rows = await cursor.fetchall()
    ids = [row[0] for row in rows]
    if not ids:
        return 0
    placeholders = ",".join(["?"] * len(ids))
    await db.execute(
        f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
        ids,
    )
    await db.execute(
        f"DELETE FROM memories WHERE id IN ({placeholders})",
        ids,
    )
    return len(ids)


async def _ingest_file(
    db: aiosqlite.Connection,
    file_path: Path,
    text: str,
    sha: str,
) -> int:
    """Insert chunks for a file into memories + queue embeddings. Returns count."""
    from potluck_local.services.memory_safety import redact_credentials

    # Redact before chunking so a private key cannot be split across records.
    chunks = chunk_markdown(redact_credentials(text))
    if not chunks:
        return 0

    embedder = get_embedder()
    bodies = [_chunk_content(c, file_path) for c in chunks]
    vectors = await embedder.embed_many(bodies)
    # If embedder is unavailable we still insert the chunks (graph will show
    # them as orphans) — better than silently dropping the user's content.

    now = _now_iso()
    inserted_ids: list[tuple[str, list]] = []  # (id, vector-or-None)

    from potluck_local.services.ai_contracts import VAULT_INGESTION_VERSION

    for chunk, body in zip(chunks, bodies):
        mem_id = str(uuid.uuid4())
        source_ref = f"vault:{file_path}#{chunk.anchor}"
        await db.execute(
            """INSERT INTO memories
               (id, content, category, confidence, source_type,
                source_conversation_id, source_ref,
                created_at, last_accessed_at, updated_at,
                access_count, is_active, project,
                extraction_prompt_version)
               VALUES (?, ?, 'reference', 0.6, 'vault', NULL, ?, ?, ?, ?, 0, 1, NULL, ?)""",
            (mem_id, body, source_ref, now, now, now, VAULT_INGESTION_VERSION),
        )
        inserted_ids.append((mem_id, body))

    # Persist embeddings (must happen after the inserts have committed-ish;
    # we keep the same connection so it's all in one transaction).
    if vectors is not None:
        for (mem_id, _body), vec in zip(inserted_ids, vectors):
            await db.execute(
                """INSERT OR REPLACE INTO memory_embeddings
                   (memory_id, embedding, model_used) VALUES (?, ?, ?)""",
                (mem_id, vec.tobytes(), embedder.MODEL_NAME),
            )

    # Upsert vault_files row
    await db.execute(
        """INSERT INTO vault_files (file_path, content_sha, chunk_count, last_ingested_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
             content_sha = excluded.content_sha,
             chunk_count = excluded.chunk_count,
             last_ingested_at = excluded.last_ingested_at""",
        (str(file_path), sha, len(chunks), now),
    )
    return len(chunks)


async def scan_vault(folder: Path) -> ScanResult:
    """Full scan: ingest new + changed files, reap chunks for removed files.

    Idempotent. Returns counts. Caller is responsible for updating settings
    (last_scan_at, file_count, chunk_count) from the result.
    """
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Vault folder does not exist: {folder}")

    md_files = sorted(folder.rglob("*.md"))
    db_path = get_memory_db_path()

    files_added = 0
    files_updated = 0
    files_removed = 0
    chunks_inserted = 0
    chunks_deleted = 0

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")

        # Load existing vault_files state
        cursor = await db.execute(
            "SELECT file_path, content_sha FROM vault_files"
        )
        rows = await cursor.fetchall()
        known: dict[str, str] = {row[0]: row[1] for row in rows}

        on_disk: set[str] = set()

        for path in md_files:
            on_disk.add(str(path))
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("vault_read_skip path=%s reason=%s", path, exc)
                continue

            sha = _sha256(text)
            existing_sha = known.get(str(path))
            if existing_sha == sha:
                continue  # unchanged

            if existing_sha is not None:
                # Updated file: reap its old chunks first
                deleted = await _delete_chunks_for_file(db, str(path))
                chunks_deleted += deleted
                files_updated += 1
            else:
                files_added += 1

            inserted = await _ingest_file(db, path, text, sha)
            chunks_inserted += inserted

        # Reap files that have disappeared from disk
        for known_path in known:
            if known_path not in on_disk:
                deleted = await _delete_chunks_for_file(db, known_path)
                await db.execute(
                    "DELETE FROM vault_files WHERE file_path = ?",
                    (known_path,),
                )
                chunks_deleted += deleted
                files_removed += 1

        await db.commit()

    logger.info(
        "vault_scan_complete folder=%s scanned=%d added=%d updated=%d removed=%d "
        "chunks_inserted=%d chunks_deleted=%d",
        folder, len(md_files), files_added, files_updated, files_removed,
        chunks_inserted, chunks_deleted,
    )

    return ScanResult(
        files_scanned=len(md_files),
        files_added=files_added,
        files_updated=files_updated,
        files_removed=files_removed,
        chunks_inserted=chunks_inserted,
        chunks_deleted=chunks_deleted,
    )


async def get_vault_status() -> dict:
    """Return current vault stats from the DB (independent of settings)."""
    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(chunk_count), 0), MAX(last_ingested_at) "
            "FROM vault_files"
        )
        row = await cursor.fetchone()
    return {
        "file_count": row[0] if row else 0,
        "chunk_count": row[1] if row else 0,
        "last_scan_at": row[2] if row and row[2] else None,
    }


# A single global lock so concurrent rescan triggers serialise rather than
# corrupting vault_files state. The lock is module-scoped so it survives
# request boundaries.
_scan_lock = asyncio.Lock()


async def scan_vault_locked(folder: Path) -> ScanResult:
    """scan_vault wrapped in a global lock — call this from API handlers."""
    async with _scan_lock:
        return await scan_vault(folder)
