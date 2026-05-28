"""Vault router — markdown folder ingestion endpoints.

Mounted at /vault by app.py.

GET    /vault/status   — current folder + ingestion stats (file/chunk counts)
POST   /vault/rescan   — re-ingest the configured folder (idempotent)
DELETE /vault          — wipe all vault content from memories + clear config

Folder configuration uses the standard PATCH /settings flow with
{"vault": {"folder": "/abs/path"}}. The dedicated endpoints below operate
against the currently-configured folder.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from potluck_local.services.settings_manager import get_settings, save_settings
from potluck_local.services.vault_ingestion import (
    get_vault_status,
    scan_vault_locked,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def vault_status() -> dict:
    """Return the configured folder + live DB stats.

    Stats come from the DB rather than settings so they remain accurate even
    if a previous scan crashed before persisting back to settings.
    """
    s = get_settings()
    db_stats = await get_vault_status()
    return {
        "folder": s.vault.folder,
        "last_scan_at": db_stats["last_scan_at"] or s.vault.last_scan_at,
        "file_count": db_stats["file_count"],
        "chunk_count": db_stats["chunk_count"],
    }


@router.post("/rescan")
async def vault_rescan() -> dict:
    """Re-ingest the configured vault folder.

    Returns the scan summary. Errors:
      - 400 if no folder configured
      - 400 if folder doesn't exist or isn't a directory
    """
    s = get_settings()
    if not s.vault.folder:
        raise HTTPException(
            status_code=400,
            detail="No vault folder configured. PATCH /settings with vault.folder first.",
        )

    folder = Path(s.vault.folder).expanduser()
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Vault folder does not exist or isn't a directory: {folder}",
        )

    try:
        result = await scan_vault_locked(folder)
    except Exception:
        logger.exception("vault_rescan_failed", extra={"event": "vault_rescan_failed"})
        raise HTTPException(
            status_code=500,
            detail="Vault scan failed — check sidecar logs",
        )

    # Persist updated stats back to settings so the UI shows them even before
    # the next /status call.
    db_stats = await get_vault_status()
    s.vault.last_scan_at = db_stats["last_scan_at"]
    s.vault.file_count = db_stats["file_count"]
    s.vault.chunk_count = db_stats["chunk_count"]
    save_settings(s)

    return {
        "files_scanned": result.files_scanned,
        "files_added": result.files_added,
        "files_updated": result.files_updated,
        "files_removed": result.files_removed,
        "chunks_inserted": result.chunks_inserted,
        "chunks_deleted": result.chunks_deleted,
        "file_count": db_stats["file_count"],
        "chunk_count": db_stats["chunk_count"],
        "last_scan_at": db_stats["last_scan_at"],
    }


@router.delete("")
async def vault_disconnect() -> dict:
    """Clear all vault content from the memory store and forget the folder.

    Reaps every memory with source_type='vault', clears vault_files, and
    resets vault settings to defaults. Non-vault memories are untouched.
    """
    import aiosqlite

    from potluck_local.services.memory_db import get_memory_db_path

    db_path = get_memory_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        cursor = await db.execute(
            "SELECT id FROM memories WHERE source_type = 'vault'"
        )
        rows = await cursor.fetchall()
        ids = [row[0] for row in rows]
        if ids:
            placeholders = ",".join(["?"] * len(ids))
            await db.execute(
                f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
                ids,
            )
            await db.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                ids,
            )
        await db.execute("DELETE FROM vault_files")
        await db.commit()

    s = get_settings()
    s.vault.folder = None
    s.vault.last_scan_at = None
    s.vault.file_count = 0
    s.vault.chunk_count = 0
    save_settings(s)

    logger.info("vault_disconnected reaped_chunks=%d", len(ids))
    return {"reaped_chunks": len(ids)}
