import asyncio
import sqlite3

import pytest

from potluck_local.services import reviewed_knowledge as review
from potluck_local.services import source_collections as sources
from potluck_local.services.memory_db import init_memory_db


def test_review_lifecycle_and_source_revocation(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    file = folder / 'runbook.md'
    file.write_text('# Runbook\n\nOriginal evidence')

    async def run():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        with sqlite3.connect(path) as db:
            doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
            chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
        ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
        scope = [collection]
        first = await review.propose_claim('w', scope, 'key', 'first', [ref])
        assert await review.list_claims('w', scope) == []
        with pytest.raises(ValueError):
            await review.approve_claim('w', scope, first, '', 'reason')
        await review.approve_claim('w', scope, first, 'reviewer', 'verified')
        second = await review.propose_claim('w', scope, 'key', 'corrected', [ref])
        with pytest.raises(ValueError):
            await review.approve_claim('w', scope, second, 'reviewer', 'verified')
        await review.supersede_claim('w', scope, first, second, 'reviewer', 'corrected')
        assert [r['content'] for r in await review.list_claims('w', scope)] == ['corrected']
        assert await review.list_claims('other', scope) == []
        assert await review.list_claims('w', ['other']) == []
        await review.delete_claim('w', scope, second)
        assert await review.list_claims('w', scope) == []
        assert [r['content'] for r in await review.list_claims('w', scope, 'superseded')] == ['first']
        file.write_text('# New\n\nChanged evidence')
        await sources.scan_collection('w', collection)
        assert await review.list_claims('w', scope, 'superseded') == []
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM reviewed_claims WHERE status != 'deleted'").fetchone()[0] == 0
        with pytest.raises(ValueError):
            await review.propose_claim('w', scope, 'key', 'stale', [ref])
    asyncio.run(run())


def test_failed_scan_and_exclusions_scrub_history(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    file = folder / 'runbook.md'
    file.write_text('# Evidence\n\nData')

    async def run():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        with sqlite3.connect(path) as db:
            doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
            chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
        ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
        claim = await review.propose_claim('w', [collection], 'key', 'secret', [ref])
        await review.approve_claim('w', [collection], claim, 'r', 'checked')
        file.write_bytes(b'\xff')
        with pytest.raises(UnicodeError):
            await sources.scan_collection('w', collection)
        assert await review.list_claims('w', [collection]) == []
        await sources.register_collection('w', 'docs', folder, excludes=('runbook.md',))
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM reviewed_claims WHERE status != 'deleted'").fetchone()[0] == 0
    asyncio.run(run())


def test_failed_scan_delete_and_tombstone(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    file = folder / 'a.md'
    file.write_text('# Evidence\n\nData')

    async def run():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        with sqlite3.connect(path) as db:
            doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
            chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
        ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
        claim = await review.propose_claim('w', [collection], 'key', 'private', [ref])
        await review.approve_claim('w', [collection], claim, 'person', 'reason')
        file.write_bytes(b'\xff')
        with pytest.raises(UnicodeError):
            await sources.scan_collection('w', collection)
        with pytest.raises(LookupError):
            await review.delete_claim('w', ['wrong'], claim)
        await review.delete_claim('w', [collection], claim)
        deleted = await review.list_claims('w', [collection], 'deleted')
        assert deleted[0]['content'] == deleted[0]['claim_key'] == ''
        assert deleted[0]['reviewer'] is None
        await sources.remove_collection('w', collection)
        assert await review.list_claims('w', [collection], 'deleted') == []
    asyncio.run(run())


def test_supersession_link_and_database_uniqueness(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'a.md').write_text('# Evidence\n\nData')

    async def run():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        with sqlite3.connect(path) as db:
            doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
            chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
        ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
        first = await review.propose_claim('w', [collection], 'key', 'first', [ref])
        second = await review.propose_claim('w', [collection], 'key', 'second', [ref])
        other = await review.propose_claim('w', [collection], 'other', 'other', [ref])
        await review.approve_claim('w', [collection], first, 'r', 'checked')
        with pytest.raises(ValueError):
            await review.supersede_claim('w', [collection], first, other, 'r', 'wrong')
        assert (await review.list_claims('w', [collection]))[0]['id'] == first
        with sqlite3.connect(path) as db:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE reviewed_claims SET status='approved' WHERE id=?", (second,))
        await review.supersede_claim('w', [collection], first, second, 'r', 'corrected')
        history = await review.list_claims('w', [collection], 'superseded')
        assert history[0]['superseded_by'] == second
        assert history[0]['superseded_reason'] == 'corrected'
        assert history[0]['superseded_at']
        await review.delete_claim('w', [collection], second)
        assert await review.list_claims('w', [collection]) == []
    asyncio.run(run())


def test_multiple_sources_scope_and_redaction(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    secret = 'sk-proj-' + 'Synthetic09_' * 4

    async def run():
        await init_memory_db()
        collections = []
        for name in ('a', 'b'):
            folder = tmp_path / name
            folder.mkdir()
            (folder / 'a.md').write_text('# Evidence\n\nData')
            collection = await sources.register_collection('w', name, folder)
            await sources.scan_collection('w', collection)
            collections.append(collection)
        with sqlite3.connect(path) as db:
            rows = db.execute(
                'SELECT d.collection_id,d.id,d.revision_id,k.id FROM source_documents d '
                'JOIN source_chunks k ON k.document_id=d.id ORDER BY d.collection_id').fetchall()
        refs = [dict(zip(('collection_id', 'document_id', 'revision_id', 'chunk_id'), row))
                for row in rows]
        with pytest.raises(ValueError):
            await review.propose_claim('w', collections[:1], 'key', 'text', refs)
        claim = await review.propose_claim('w', collections, secret, secret, refs)
        await review.approve_claim('w', collections, claim, secret, secret)
        assert await review.list_claims('w', collections[:1]) == []
        result = (await review.list_claims('w', collections))[0]
        assert all(secret not in result[field]
                   for field in ('claim_key', 'content', 'reviewer', 'reason'))
        with pytest.raises(ValueError):
            await review.list_claims('w', [None])
        with pytest.raises(ValueError):
            await review.list_claims('w', 'not-a-list')
    asyncio.run(run())


def test_supersession_rollback_and_cli(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys

    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'a.md').write_text('# Evidence\n\nData')

    async def setup():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        return collection

    collection = asyncio.run(setup())
    with sqlite3.connect(path) as db:
        doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
        chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
    ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
    base = [sys.executable, '-m', 'potluck_local.services.reviewed_knowledge',
            '--workspace', 'w', '--collection', collection]
    env = {**os.environ, 'PYTHONPATH': '.'}
    result = subprocess.run(base + ['propose', 'key', 'text', '--citation', json.dumps(ref)],
                            env=env, check=True, capture_output=True, text=True)
    first = json.loads(result.stdout)['id']
    subprocess.run(base + ['approve', first, '--reviewer', 'r', '--reason', 'checked'],
                   env=env, check=True, capture_output=True, text=True)
    result = subprocess.run(base + ['propose', 'key', 'replacement', '--citation', json.dumps(ref)],
                            env=env, check=True, capture_output=True, text=True)
    second = json.loads(result.stdout)['id']
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_replacement BEFORE UPDATE ON reviewed_claims "
                   "WHEN NEW.id = '" + second + "' AND NEW.status='approved' "
                   "BEGIN SELECT RAISE(ABORT, 'forced'); END")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(review.supersede_claim('w', [collection], first, second, 'r', 'reason'))
    assert asyncio.run(review.list_claims('w', [collection]))[0]['id'] == first
    result = subprocess.run(base + ['list'], env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)[0]['id'] == first


def test_invalid_citations_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'a.md').write_text('# Evidence\n\nData')

    async def run():
        await init_memory_db()
        collection = await sources.register_collection('w', 'docs', folder)
        await sources.scan_collection('w', collection)
        with sqlite3.connect(path) as db:
            doc, rev = db.execute('SELECT id,revision_id FROM source_documents').fetchone()
            chunk = db.execute('SELECT id FROM source_chunks').fetchone()[0]
        ref = dict(collection_id=collection, document_id=doc, revision_id=rev, chunk_id=chunk)
        for refs in ([], [ref, ref], [ref] * 17, [{**ref, 'chunk_id': 12}]):
            with pytest.raises(ValueError):
                await review.propose_claim('w', [collection], 'key', 'text', refs)
        with pytest.raises(ValueError):
            await review.propose_claim('w', [collection], 'key', 'x' * 16001, [ref])
        with pytest.raises(ValueError):
            await review.list_claims('w', [collection, collection])
    asyncio.run(run())
