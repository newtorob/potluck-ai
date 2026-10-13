"""Purge pre-existing soft-deleted memories whose content still lingers on disk.

Memories deleted by older releases were only marked is_active=0 - the row (and
its content) stays in memory.db and can still be surfaced (e.g. in the /agents
"memories touched" debugger). This is a one-time cleanup of those rows.

SAFETY:
  - Only purges rows with is_active=0 AND superseded_by IS NULL. Temporal
    versioning marks old versions with is_active=0 + superseded_by set; those
    are version history, not user deletes, and are left untouched.
  - Dry-run by default (prints what it would remove). Pass --apply to delete.
  - With --apply it also deletes the matching memory_embeddings rows and runs
    VACUUM so the freed bytes are reclaimed (content not recoverable from free
    pages).

Usage:
  .venv/bin/python -m scripts.purge_soft_deleted            # dry run
  .venv/bin/python -m scripts.purge_soft_deleted --apply    # delete + VACUUM
"""

from __future__ import annotations

import asyncio
import sys

import aiosqlite

from potluck_local.services.memory_db import get_memory_db_path

_SELECT_IDS = "SELECT id FROM memories WHERE is_active = 0 AND superseded_by IS NULL"


async def main() -> int:
    apply = "--apply" in sys.argv[1:]
    db_path = str(get_memory_db_path())

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cols = [r["name"] for r in await (await db.execute("PRAGMA table_info(memories)")).fetchall()]
        if "superseded_by" not in cols:
            print("ERROR: no superseded_by column, refusing to guess at version history.", file=sys.stderr)
            return 2

        ids = [r["id"] for r in await (await db.execute(_SELECT_IDS)).fetchall()]
        versioned = (
            await (
                await db.execute(
                    "SELECT COUNT(*) n FROM memories WHERE is_active = 0 AND superseded_by IS NOT NULL"
                )
            ).fetchone()
        )["n"]

        print(f"DB: {db_path}")
        print(f"True-delete rows to purge (is_active=0, superseded_by NULL): {len(ids)}")
        print(f"Version-history rows preserved (superseded_by set):          {versioned}")

        if not ids:
            print("Nothing to purge.")
            return 0

        if not apply:
            print("\nDry run: nothing deleted. Re-run with --apply to purge + VACUUM.")
            return 0

        placeholders = ",".join("?" for _ in ids)
        await db.execute(f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})", ids)  # noqa: S608
        cur = await db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)  # noqa: S608
        await db.commit()
        deleted = cur.rowcount
        print(f"\nDeleted {deleted} memories + their embeddings. Running VACUUM...")
        await db.execute("VACUUM")
        await db.commit()

    print("Done: freed space reclaimed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
