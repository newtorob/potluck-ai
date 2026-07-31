"""Regression test — 2026-07-31 P2 sidecar audit, finding #6.

POST /memory/import checked whether the exported id already existed and, if
not, called MemoryExtractor.extract_explicit() to insert it — which always
mints a brand-new uuid4 id and fresh created_at/last_accessed_at/updated_at,
and never carries over pinned or access_count. Re-importing the SAME export
therefore never matched the (different, freshly-minted) id already in the
DB, so it inserted a SECOND row every time — the auditor's repro: importing
the same export twice produced two rows, with the original's pin state and
timestamps lost.

The fix: MemoryExtractor.import_memory() upserts keyed on the exported id.
An id that already exists locally is updated in place (id/created_at
preserved); an id that doesn't exist is inserted with its original
id/created_at/pinned/access_count intact. Re-running the same import is
idempotent — one row, not two.

Isolated via POTLUCK_MEMORY_DB_PATH + POTLUCK_DATA_DIR (settings/config.json
also now honours POTLUCK_DATA_DIR per finding #10) so this test can never
touch a real ~/.potluck.
"""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.memory_extraction import get_memory_extractor

LOCAL = ("127.0.0.1", 50000)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data_dir"))
    _run(init_memory_db(db_path))
    return db_path


@pytest.fixture()
def client(isolated_env):
    app = create_app()
    with TestClient(app, client=LOCAL) as c:
        yield c


async def _row_count(db_path) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM memories")
        row = await cursor.fetchone()
        return row[0]


async def _fetch_row(db_path, memory_id) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
        row = await cursor.fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Route-level: the auditor's exact repro — export, import twice.
# ---------------------------------------------------------------------------


def test_reimporting_the_same_export_does_not_duplicate(client, isolated_env):
    create_resp = client.post(
        "/memory",
        json={"content": "always use pnpm, not npm", "category": "instruction"},
    )
    assert create_resp.status_code == 201
    memory_id = create_resp.json()["id"]

    pin_resp = client.post(f"/memory/{memory_id}/pin", json={"pinned": True})
    assert pin_resp.status_code == 200

    export_resp = client.get("/memory/export")
    assert export_resp.status_code == 200
    exported = export_resp.json()["memories"]
    assert len(exported) == 1
    exported_row = exported[0]
    assert exported_row["id"] == memory_id
    assert exported_row["pinned"] is True
    original_created_at = exported_row["created_at"]

    assert _run(_row_count(isolated_env)) == 1

    # Import #1: same id already present -> update in place, still ONE row.
    import_resp_1 = client.post("/memory/import", json={"memories": [exported_row]})
    assert import_resp_1.status_code == 200
    assert import_resp_1.json()["imported"] == 1
    assert _run(_row_count(isolated_env)) == 1

    # Import #2 (the auditor's "import the same export twice"): still ONE row.
    import_resp_2 = client.post("/memory/import", json={"memories": [exported_row]})
    assert import_resp_2.status_code == 200
    assert import_resp_2.json()["imported"] == 1
    assert _run(_row_count(isolated_env)) == 1

    # Pin state + original created_at must have survived both re-imports.
    row = _run(_fetch_row(isolated_env, memory_id))
    assert row["id"] == memory_id
    assert bool(row["pinned"]) is True
    assert row["created_at"] == original_created_at

    get_resp = client.get(f"/memory/{memory_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["id"] == memory_id
    assert body["pinned"] is True
    assert body["created_at"] == original_created_at
    assert body["content"] == "always use pnpm, not npm"


def test_import_of_unknown_id_restores_original_id_and_pinned_state(client, isolated_env):
    """Restoring an export onto a fresh/other instance: the row didn't exist
    locally yet, so import must INSERT it with the exported id/created_at/
    pinned preserved — not a freshly minted id and reset metadata."""
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "content": "the vault lives at ~/notes",
        "category": "fact",
        "confidence": 0.95,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_accessed_at": "2026-01-01T00:00:00+00:00",
        "pinned": True,
        "access_count": 7,
        "project": None,
    }
    resp = client.post("/memory/import", json={"memories": [payload]})
    assert resp.status_code == 200
    assert resp.json() == {"imported": 1, "skipped": 0}
    assert _run(_row_count(isolated_env)) == 1

    row = _run(_fetch_row(isolated_env, payload["id"]))
    assert row["id"] == payload["id"]
    assert row["created_at"] == payload["created_at"]
    assert bool(row["pinned"]) is True
    assert row["access_count"] == 7


# ---------------------------------------------------------------------------
# Service-level: MemoryExtractor.import_memory() directly.
# ---------------------------------------------------------------------------


def test_import_memory_service_upsert_is_idempotent(isolated_env):
    extractor = get_memory_extractor()

    raw = {
        "id": "22222222-2222-2222-2222-222222222222",
        "content": "decided to use Postgres over SQLite",
        "category": "decision",
        "confidence": 0.9,
        "pinned": False,
    }

    first = _run(extractor.import_memory(raw))
    assert first.id == raw["id"]
    assert _run(_row_count(isolated_env)) == 1

    # Re-import the identical payload — must update the same row, not insert
    # a second one.
    second = _run(extractor.import_memory(raw))
    assert second.id == raw["id"]
    assert second.created_at == first.created_at
    assert _run(_row_count(isolated_env)) == 1

    # A third import with updated content + pinned=True updates in place too.
    raw_updated = dict(raw, content="decided to use Postgres over SQLite (confirmed)", pinned=True)
    third = _run(extractor.import_memory(raw_updated))
    assert third.id == raw["id"]
    assert third.created_at == first.created_at
    assert third.pinned is True
    assert third.content == raw_updated["content"]
    assert _run(_row_count(isolated_env)) == 1
