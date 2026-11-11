"""Integration invariants across review, scanning, and evidence retrieval."""

import asyncio
import sqlite3

import pytest

from potluck_local.services import reviewed_knowledge as review
from potluck_local.services import source_collections as sources
from potluck_local.services.evidence_lookup import EvidenceScope, lookup_evidence, resolve_citation
from potluck_local.services.memory_db import init_memory_db


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'memory.db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(path))
    asyncio.run(init_memory_db())
    return path


async def seeded(tmp_path):
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'a.md').write_text('# Deployment\n\nAttribution deployed.')
    cid = await sources.register_collection('owner', 'docs', folder)
    await sources.scan_collection('owner', cid)
    scope = EvidenceScope('owner', (cid,))
    source = (await lookup_evidence(scope, 'attribution'))['source_chunks'][0]
    ref = {key: source[key] for key in
           ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
    return folder, scope, source, ref


def test_concurrent_approvals_allow_only_one_current_claim(store, tmp_path):
    async def run():
        _, scope, _, ref = await seeded(tmp_path)
        ids = [await review.propose_claim('owner', scope.collection_ids, 'deployment', text, [ref])
               for text in ('Attribution deployed', 'Attribution pending')]
        results = await asyncio.gather(*(
            review.approve_claim('owner', scope.collection_ids, cid, 'owner', 'checked')
            for cid in ids), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert len(await review.list_claims('owner', scope.collection_ids)) == 1
    asyncio.run(run())


def test_scan_failure_rolls_back_claim_invalidation_but_hides_evidence(store, tmp_path):
    async def run():
        folder, scope, source, ref = await seeded(tmp_path)
        cid = scope.collection_ids[0]
        claim = await review.propose_claim('owner', [cid], 'deployment', 'Attribution live', [ref])
        await review.approve_claim('owner', [cid], claim, 'owner', 'checked')
        (folder / 'a.md').write_text('# Deployment\n\nAttribution corrected.')
        (folder / 'z.md').write_bytes(b'\xff')
        with pytest.raises(UnicodeError):
            await sources.scan_collection('owner', cid)
        with sqlite3.connect(store) as db:
            assert db.execute('SELECT status FROM reviewed_claims WHERE id=?', (claim,)
                              ).fetchone()[0] == 'approved'
        assert await review.list_claims('owner', [cid]) == []
        assert await resolve_citation(scope, source['citation_id']) is None
        (folder / 'z.md').unlink()
        await sources.scan_collection('owner', cid)
        assert not (await lookup_evidence(scope, 'attribution'))['reviewed_claims']
        deleted = (await review.list_claims('owner', [cid], 'deleted'))[0]
        assert deleted['content'] == '' and deleted['deletion_cause'] == 'source_changed'
    asyncio.run(run())


def test_personal_pins_never_fill_missing_evidence_and_wrong_owner_cannot_revoke(store, tmp_path):
    async def run():
        _, scope, source, ref = await seeded(tmp_path)
        cid = scope.collection_ids[0]
        claim = await review.propose_claim('owner', [cid], 'deployment', 'Attribution live', [ref])
        await review.approve_claim('owner', [cid], claim, 'owner', 'checked')
        with sqlite3.connect(store) as db:
            db.execute("INSERT INTO memories (id,content,category,source_type,created_at,"
                       "last_accessed_at,updated_at,project,pinned) VALUES "
                       "('personal','UniquePersonalMarker','fact','explicit','now','now',"
                       "'now',NULL,1)")
        result = await lookup_evidence(scope, 'UniquePersonalMarker')
        assert not result['source_chunks'] and not result['reviewed_claims']
        assert result['missing_evidence']
        with pytest.raises(LookupError):
            await sources.remove_collection('other', cid)
        assert await resolve_citation(scope, source['citation_id']) is not None
        assert (await review.list_claims('owner', [cid]))[0]['id'] == claim
        await sources.remove_collection('owner', cid)
        assert await resolve_citation(scope, source['citation_id']) is None
        with sqlite3.connect(store) as db:
            assert db.execute('SELECT content FROM memories').fetchone()[0] == 'UniquePersonalMarker'
            assert db.execute('SELECT content FROM reviewed_claims').fetchone()[0] == ''
    asyncio.run(run())
