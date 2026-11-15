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


def test_opt_in_heading_context_preserves_empty_qualifiers_and_short_section_labels(store, tmp_path):
    from potluck_local.services import claim_source_context, evidence_lookup, pilot_setup_drafts
    async def run():
        folder = document(tmp_path, body='# Proposed policy, not enabled\n\n## Access\nApproval required.\n\n## Exception only after review\nNo customer rows.\n')
        cid = await sources.register_collection('acme', 'policy', folder)
        await sources.scan_collection('acme', cid)
        original = rows(store, 'source_documents')[0]
        assert all(row['heading'] != 'Proposed policy, not enabled' for row in rows(store, 'source_chunks'))
        await sources.scan_collection('acme', cid, preserve_heading_context=True)
        updated = rows(store, 'source_documents')[0]
        assert updated['revision_id'] != original['revision_id']
        assert updated['parser_version'] == sources.HEADING_CONTEXT_PARSER_VERSION
        headings = {row['heading'] for row in rows(store, 'source_chunks')}
        assert headings == {'Proposed policy, not enabled', 'Access', 'Exception only after review'}
        assert all(row['content'] for row in rows(store, 'source_chunks'))
        scope = evidence_lookup.EvidenceScope('acme', (cid,))
        draft = (await pilot_setup_drafts.source_drafts(scope, 'What access exception applies?'))['candidates'][0]['preview']
        assert all(value in draft['content'] for value in headings)
        assert draft['content'].count('Proposed policy, not enabled') == 1
        assert 'Approval required.' in draft['content'] and 'No customer rows.' in draft['content']
        from potluck_local.services.investigation_reviewed_claim_selection_v2 import prepare
        support = {row['citation_id']: row for doc in draft['documents'] for row in doc['opening'] + doc['excerpts']}
        prepared = prepare('Which planned access exception applies?', [{'kind': 'reviewed_claim',
            'citation_id': 'claim:fixture', 'content': draft['content'], 'sources': list(support.values())}])
        assert prepared['claims'][0]['text'] == draft['content']
        again = await sources.scan_collection('acme', cid, preserve_heading_context=True)
        assert again.files_updated == again.chunks_inserted == 0
        await sources.scan_collection('acme', cid)
        assert rows(store, 'source_documents')[0]['parser_version'] == sources.PARSER_VERSION
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


def test_scan_report_survives_reopen_and_failed_scan_without_reapproving_changes(store, tmp_path):
    from potluck_local.services.investigations import ensure_schema
    import aiosqlite
    import json

    async def run():
        folder = document(tmp_path)
        (folder / 'stable.md').write_text('# Stable\n\nKeep the privacy review.')
        cid = await sources.register_collection('acme', 'docs', folder)
        first = await sources.scan_collection('acme', cid)
        assert first.files_added == 2 and first.revoked_claims == 0
        chunks = rows(store, 'source_chunks')
        docs = {row['id']: row for row in rows(store, 'source_documents')}
        # Fixture approvals are explicit, not generated by scanning.
        for chunk in chunks:
            doc = docs[chunk['document_id']]
            with sqlite3.connect(store) as db:
                key = doc['relative_path']
                db.execute("INSERT INTO reviewed_claims(id,workspace,claim_key,content,status,created_at) VALUES (?,?,?,?,?,?)",
                           (key, 'acme', key, chunk['content'], 'approved', sources._now()))
                db.execute('INSERT INTO reviewed_claim_sources VALUES (?,?,?,?,?)',
                           (key, cid, doc['id'], doc['revision_id'], chunk['id']))
        async with aiosqlite.connect(store) as db:
            await ensure_schema(db)
            await db.execute('INSERT INTO investigations(id,workspace,scope,issue,model_slug,status,brief,evidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                             ('old', 'acme', json.dumps([cid]), 'Prior question', 'fixture', 'draft', '{}', '[]', sources._now()))
            await db.commit()
        unchanged = await sources.scan_collection('acme', cid)
        assert unchanged.files_unchanged == 2
        assert unchanged.changes == [] and unchanged.invalidated_answers == unchanged.revoked_claims == 0
        assert all(row['status'] == 'approved' for row in rows(store, 'reviewed_claims'))
        (folder / 'runbook.md').write_text('# Updated\n\nReview the new exception first.')
        changed = await sources.scan_collection('acme', cid)
        assert changed.files_updated == changed.files_unchanged == 1
        assert changed.invalidated_answers == changed.revoked_claims == 1
        assert changed.changes == [{'relative_path': 'runbook.md', 'change': 'updated'}]
        statuses = {row['id']: row['status'] for row in rows(store, 'reviewed_claims')}
        assert statuses == {'runbook.md': 'deleted', 'stable.md': 'approved'}
        prior = rows(store, 'investigations')[0]
        assert prior['status'] == 'invalidated' and prior['brief'] == prior['issue'] == prior['evidence'] == ''
        # A fresh connection restores the receipt; a failed scan cannot replace it.
        receipt = json.loads(rows(store, 'source_scan_reports')[0]['report'])
        assert receipt['scanned_at'] == changed.scanned_at
        assert receipt['changes'] == changed.changes
        (folder / 'bad.md').write_bytes(b'\xff')
        with pytest.raises(UnicodeError):
            await sources.scan_collection('acme', cid)
        assert json.loads(rows(store, 'source_scan_reports')[0]['report']) == receipt
        assert rows(store, 'source_collections')[0]['scan_status'] == 'failed'
        await sources.remove_collection('acme', cid)
        assert rows(store, 'source_scan_reports') == []
    asyncio.run(run())


def test_scan_report_bounds_paths_but_keeps_complete_counts(store, tmp_path):
    async def run():
        folder = document(tmp_path)
        for i in range(105):
            (folder / f'{i:03}.md').write_text(f'# Rule {i}\n\nRequire review.')
        cid = await sources.register_collection('acme', 'docs', folder)
        report = await sources.scan_collection('acme', cid)
        assert report.files_added == 106
        assert len(report.changes) == 100 and report.changes_truncated
        report = await sources.scan_collection('acme', cid, preserve_heading_context=True)
        assert report.files_updated == 106
        assert all(row['change'] == 'reindexed' for row in report.changes)
        (folder / 'runbook.md').unlink()
        report = await sources.scan_collection('acme', cid, preserve_heading_context=True)
        assert report.files_removed == 1 and report.files_unchanged == 105
        assert report.changes == [{'relative_path': 'runbook.md', 'change': 'removed'}]
    asyncio.run(run())
