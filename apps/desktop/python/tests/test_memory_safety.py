"""Credential fixtures are synthetic. Every write uses a disposable database."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import numpy as np
import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import embeddings, memory_extraction, memory_retrieval, settings_manager
from potluck_local.services.memory_db import init_memory_db, save_embedding
from potluck_local.services.memory_safety import REDACTED, redact_credentials

SYNTHETIC = "shpss_" + "a1b2c3d4" * 4


@pytest.mark.parametrize("value", [
    SYNTHETIC, "shpat_" + "d4c3b2a1" * 4, "hf_" + "TestToken09" * 4,
    "sk-proj-" + "Synthetic09_" * 4, "sk-ant-api03-" + "Synthetic09_" * 4,
    "ghp_" + "Synthetic09" * 4, "github_pat_" + "Synthetic09_" * 4,
    "rnd_" + "Synthetic09" * 4, "sb_secret_" + "Synthetic09_" * 4,
    "xoxb-" + "1234567890-" * 4, "AKIA" + "A1B2" * 4,
    "eyJ" + "aB19" * 4 + "." + "cD29" * 4 + "." + "eF39" * 4,
    "-----BEGIN PRIVATE KEY-----\n" + "abc123" * 8 + "\n-----END PRIVATE KEY-----",
])
def test_recognizable_credentials_are_redacted(value):
    text = f"Deployment credential {value}; keep the deployment instructions."
    safe = redact_credentials(text)
    assert value not in safe
    assert REDACTED in safe
    assert safe.endswith("; keep the deployment instructions.")
    assert redact_credentials(safe) == safe


@pytest.mark.parametrize("text,value", [
    ('{"api_key": "Synthetic09abcd1234"}', "Synthetic09abcd1234"),
    ("COORDINATOR_TOKEN=abcd1234efgh5678", "abcd1234efgh5678"),
    ("password: swordfish", "swordfish"),
    ("Authorization: Bearer Synthetic09abcd1234", "Synthetic09abcd1234"),
])
def test_labelled_credentials_are_redacted(text, value):
    assert value not in redact_credentials(text)


@pytest.mark.parametrize("text", [
    "Auth requires a Bearer token in the Authorization header.",
    "Never store secrets or passwords in memory.",
    "Set API_KEY=${API_KEY} and password: <your-password>.",
    "api_key: your_api_key and token: authentication",
    "The token is bucket-scoped; API_KEY=SHOPIFY_API_KEY is an environment reference.",
    "Use token: read-only and api_key: process.env.SHOPIFY_API_KEY in examples.",
    "Client id 0123456789abcdef0123456789abcdef is public.",
    "VITE_SHOPIFY_API_KEY=0123456789abcdef0123456789abcdef is a public client id.",
    "Commit 0123456789abcdef0123456789abcdef01234567 fixes downloads.",
    "/Users/sam/project/token_manager.py uses port 8321.",
    "Task 123e4567-e89b-42d3-a456-426614174000 is active.",
])
def test_harmless_references_and_identifiers_are_unchanged(text):
    assert redact_credentials(text) == text


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings_manager, "_settings_cache", {})
    monkeypatch.setattr(embeddings, "schedule_embed_memory", lambda *args: None)
    monkeypatch.setattr(memory_retrieval, "semantic_search", AsyncMock(return_value=[]))
    asyncio.run(init_memory_db(db))
    return db


def test_save_edit_import_search_and_export_redact_credentials(isolated):
    # Do not start background services; the schema is initialized by the fixture.
    client = TestClient(create_app(), client=("127.0.0.1", 50000))
    created = client.post("/memory", json={"content": f"Shopify secret {SYNTHETIC}", "category": "fact"})
    assert created.status_code == 201
    mid = created.json()["id"]
    assert created.json()["content"] == f"Shopify secret {REDACTED}"
    edited = client.patch(f"/memory/{mid}", json={"content": f"Deployment uses {SYNTHETIC}"})
    assert edited.status_code == 200
    imported = client.post("/memory/import", json={"memories": [{
        "id": mid, "content": f"Shopify uses {SYNTHETIC}", "category": "fact",
    }]})
    assert imported.status_code == 200
    with sqlite3.connect(isolated) as db:
        assert SYNTHETIC not in db.execute("SELECT content FROM memories WHERE id=?", (mid,)).fetchone()[0]
        # Simulate a legacy writer after startup to exercise the read defense.
        db.execute("UPDATE memories SET content=? WHERE id=?", (f"Shopify secret {SYNTHETIC}", mid))
    for response in [client.get("/memory"), client.get(f"/memory/{mid}"),
                     client.get("/memory/export"), client.get("/memory/keywords?min_count=1"),
                     client.post("/agents/memory-previews", json={"ids": [mid]}),
                     client.post("/memory/search", json={"query": "Shopify", "semantic": False})]:
        assert response.status_code == 200
        assert SYNTHETIC not in response.text
        assert SYNTHETIC.split("_", 1)[1] not in response.text


def test_startup_repairs_legacy_rows_and_invalidates_vectors(isolated):
    async def scenario():
        mem = await memory_extraction.get_memory_extractor().extract_explicit("Shopify deployment", "fact")
        await save_embedding(mem.id, np.ones(384, dtype=np.float32), "test-model")
        async with aiosqlite.connect(isolated) as db:
            await db.execute("UPDATE memories SET content=?, pinned=1 WHERE id=?", (f"Shopify secret {SYNTHETIC}", mem.id))
            await db.commit()
        await init_memory_db(isolated)
        await init_memory_db(isolated)  # idempotent
        with sqlite3.connect(isolated) as db:
            content, pinned = db.execute("SELECT content,pinned FROM memories WHERE id=?", (mem.id,)).fetchone()
            assert content == f"Shopify secret {REDACTED}"
            assert pinned == 1
            assert db.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    asyncio.run(scenario())


def test_extraction_redacts_before_model_call_and_storage(isolated, monkeypatch):
    fake = SimpleNamespace(is_loaded=lambda: True, chat=AsyncMock(return_value={"choices": [{
        "message": {"content": json.dumps([{"content": f"Shopify secret {SYNTHETIC}", "category": "fact", "confidence": 1.0}])},
    }]}))
    monkeypatch.setattr("potluck_local.services.runtime.get_runtime", lambda: fake)
    result = asyncio.run(memory_extraction.get_memory_extractor().extract_from_conversation(
        [{"role": "user", "content": f"Remember my Shopify secret {SYNTHETIC}"}], "test",
    ))
    assert result and SYNTHETIC not in result[0].content
    assert SYNTHETIC not in str(fake.chat.call_args)


def test_vault_redacts_before_chunking_and_embedding(isolated, tmp_path, monkeypatch):
    from potluck_local.services import vault_ingestion
    fake = SimpleNamespace(embed_many=AsyncMock(return_value=None))
    monkeypatch.setattr(vault_ingestion, "get_embedder", lambda: fake)
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "deployment.md").write_text(f"# Deployment\n\nShopify secret {SYNTHETIC}\n")
    asyncio.run(vault_ingestion.scan_vault(vault))
    assert SYNTHETIC not in str(fake.embed_many.call_args)
    with sqlite3.connect(isolated) as db:
        contents = [r[0] for r in db.execute("SELECT content FROM memories")]
        assert contents and all(SYNTHETIC not in text for text in contents)
