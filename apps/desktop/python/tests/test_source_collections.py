"""Source collections use disposable databases and synthetic local documents."""

import asyncio
import sqlite3

import pytest

from potluck_local.services import source_collections as sources
from potluck_local.services.memory_db import init_memory_db


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / 'memory.db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(db))
    asyncio.run(init_memory_db())
    return db


def rows(store, table):
    with sqlite3.connect(store) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')]


def document(tmp_path, name='notes', body='# Runbook\n\nCheck the deployment evidence first.'):
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    (folder / 'runbook.md').write_text(body)
    return folder


def test_collections_scan_and_remove_independently(store, tmp_path):
    async def run():
        a = await sources.register_collection('acme', 'first', document(tmp_path, 'a'))
        b = await sources.register_collection('acme', 'second', document(tmp_path, 'b'))
        await sources.scan_collection('acme', a)
        await sources.scan_collection('acme', b)
        before = rows(store, 'source_chunks')
        assert len(before) == 2
        (tmp_path / 'a' / 'runbook.md').unlink()
        result = await sources.scan_collection('acme', a)
        assert result.files_removed == 1
        assert len(rows(store, 'source_chunks')) == 1
        await sources.remove_collection('acme', a)
        assert (await sources.collection_status('acme'))[0]['id'] == b
        assert (tmp_path / 'b' / 'runbook.md').exists()
        assert rows(store, 'memories') == []
        assert rows(store, 'vault_files') == []
    asyncio.run(run())


def test_idempotence_revisions_and_empty_file(store, tmp_path):
    async def run():
        folder = document(tmp_path)
        collection = await sources.register_collection('acme', 'docs', folder)
        assert await sources.register_collection('acme', 'docs', folder) == collection
        await sources.scan_collection('acme', collection)
        original = rows(store, 'source_documents')[0]
        chunks = rows(store, 'source_chunks')
        result = await sources.scan_collection('acme', collection)
        assert result.files_added == result.files_updated == result.chunks_inserted == 0
        assert rows(store, 'source_chunks') == chunks
        (folder / 'runbook.md').write_text('# Changed\n\nUpdated procedure.')
        assert (await sources.scan_collection('acme', collection)).files_updated == 1
        changed = rows(store, 'source_documents')[0]
        assert changed['id'] == original['id']
        assert changed['revision_id'] != original['revision_id']
        assert chunks[0]['id'] != rows(store, 'source_chunks')[0]['id']
        (folder / 'runbook.md').write_text('')
        await sources.scan_collection('acme', collection)
        assert len(rows(store, 'source_documents')) == 1
        assert rows(store, 'source_chunks') == []
        assert (await sources.scan_collection('acme', collection)).files_updated == 0
    asyncio.run(run())


def test_exclusions_symlinks_and_nested_includes(store, tmp_path):
    async def run():
        folder = document(tmp_path)
        archive = folder / 'archive'
        archive.mkdir()
        (archive / 'old.md').write_text('Superseded guidance')
        (folder / 'draft.md').write_text('Unreviewed draft')
        nested = folder / 'engineering'
        nested.mkdir()
        (nested / 'current.md').write_text('Current engineering evidence')
        outside = document(tmp_path, 'outside')
        (folder / 'linked.md').symlink_to(outside / 'runbook.md')
        (folder / 'linked-dir').symlink_to(outside, target_is_directory=True)
        collection = await sources.register_collection(
            'acme', 'docs', folder, excludes=sources.DEFAULT_EXCLUDES + ('draft.md',),
        )
        assert (await sources.scan_collection('acme', collection)).files_scanned == 2
        assert {d['relative_path'] for d in rows(store, 'source_documents')} == {
            'runbook.md', 'engineering/current.md',
        }
        await sources.register_collection('acme', 'docs', folder, includes=('engineering/**',))
        assert rows(store, 'source_chunks') == []
        assert (await sources.collection_status('acme'))[0]['scan_status'] == 'pending'
        assert (await sources.scan_collection('acme', collection)).files_scanned == 1
    asyncio.run(run())


def test_wrong_workspace_cannot_address_collection(store, tmp_path):
    async def run():
        collection = await sources.register_collection('acme', 'docs', document(tmp_path))
        with pytest.raises(LookupError):
            await sources.scan_collection('other', collection)
        with pytest.raises(LookupError):
            await sources.remove_collection('other', collection)
        assert await sources.collection_status('other') == []
        assert (await sources.collection_status('acme'))[0]['scan_status'] == 'never'
    asyncio.run(run())


def test_same_folder_can_belong_to_two_workspaces(store, tmp_path):
    async def run():
        folder = document(tmp_path)
        a = await sources.register_collection('acme', 'docs', folder)
        b = await sources.register_collection('other', 'docs', folder)
        await sources.scan_collection('acme', a)
        await sources.scan_collection('other', b)
        assert len({d['id'] for d in rows(store, 'source_documents')}) == 2
        await sources.remove_collection('acme', a)
        assert (await sources.collection_status('other'))[0]['chunk_count'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['invalid_utf8', 'missing_root', 'too_large'])
def test_failed_scan_retains_atomic_snapshot_and_marks_stale(store, tmp_path, failure):
    async def run():
        folder = document(tmp_path)
        collection = await sources.register_collection('acme', 'docs', folder)
        await sources.scan_collection('acme', collection)
        before = rows(store, 'source_chunks')
        (folder / 'a-new.md').write_text('This must roll back if a later read fails.')
        if failure == 'missing_root':
            folder.rename(tmp_path / 'moved')
        elif failure == 'invalid_utf8':
            (folder / 'z.md').write_bytes(b'\xff')
        else:
            (folder / 'z.md').write_bytes(b'x' * (sources.MAX_FILE_BYTES + 1))
        with pytest.raises((OSError, ValueError)):
            await sources.scan_collection('acme', collection)
        assert rows(store, 'source_chunks') == before
        status = (await sources.collection_status('acme'))[0]
        assert status['scan_status'] == 'failed'
        assert status['last_error']
    asyncio.run(run())


def test_credentials_redacted_and_no_personal_memory_migration(store, tmp_path):
    async def run():
        with sqlite3.connect(store) as db:
            db.execute("INSERT INTO memories (id,content,category,source_type,created_at,"
                       "last_accessed_at,updated_at,project) VALUES "
                       "('personal','My preference','preference','explicit','now','now','now',NULL)")
        personal = rows(store, 'memories')
        secret = 'ghp_' + 'Synthetic09' * 4
        folder = document(tmp_path, body='# Guide\n\nToken ' + secret + '\n\nUseful procedure.')
        collection = await sources.register_collection('acme', 'docs', folder)
        await sources.scan_collection('acme', collection)
        assert secret not in str(rows(store, 'source_chunks'))
        assert 'Useful procedure' in str(rows(store, 'source_chunks'))
        await init_memory_db()
        assert rows(store, 'memories') == personal
        assert len(rows(store, 'source_documents')) == 1
    asyncio.run(run())


@pytest.mark.parametrize('pattern', ['/etc/**', '../**', '**/../secret', ''])
def test_invalid_source_patterns_rejected(store, tmp_path, pattern):
    with pytest.raises(ValueError):
        asyncio.run(sources.register_collection('acme', 'docs', document(tmp_path),
                                               includes=(pattern,)))


def test_cannot_rebind_collection_root_or_use_symlink_root(store, tmp_path):
    async def run():
        a, b = document(tmp_path, 'a'), document(tmp_path, 'b')
        await sources.register_collection('acme', 'docs', a)
        with pytest.raises(ValueError):
            await sources.register_collection('acme', 'docs', b)
        link = tmp_path / 'linked'
        link.symlink_to(a, target_is_directory=True)
        with pytest.raises(ValueError):
            await sources.register_collection('acme', 'linked', link)
    asyncio.run(run())
