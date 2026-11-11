from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import aiosqlite
import pytest
import pytest_asyncio

from potluck_local.services import investigation_collection_workflow as adapter
from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.memory_db import init_memory_db, get_memory_db_path
from potluck_local.services.source_collections import register_collection, scan_collection


@pytest_asyncio.fixture
async def indexed(tmp_path, monkeypatch):
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setattr(adapter, '_privacy', lambda: None)
    await init_memory_db(); folder = tmp_path / 'docs'; folder.mkdir()
    (folder / 'policy.md').write_text('# Policy\n\nThe deployment requires approval.\n')
    cid = await register_collection('w', 'docs', folder); await scan_collection('w', cid)
    return EvidenceScope('w', (cid,)), folder


@pytest.mark.asyncio
async def test_real_index_retrieval_receipt_and_selected_scope(indexed):
    scope, _ = indexed; r = await adapter.prepare(scope, 'What deployment approval is required?')
    assert r['prepared_workflow']['version'] == workflow.V10_INPUT_VERSION
    assert 'not collection-wide proof' in r['scope']
    assert r['sources'][0]['content'] == r['envelope']['sources'][0]['quote']
    assert r['sources'][0]['citation_id'] == r['envelope']['sources'][0]['context']['citation_id']
    assert r['snapshot']['documents'] and r['snapshot']['collections'][0]['scan_status'] == 'ready'


@pytest.mark.asyncio
async def test_receipt_tamper_and_snapshot_change_rejected(indexed, monkeypatch):
    scope, folder = indexed; receipt = await adapter.prepare(scope, 'deployment approval')
    async def fake(*args, **kwargs): return {'status': 'proposal'}
    monkeypatch.setattr(workflow, 'generate', fake)
    assert (await adapter.generate(scope, receipt, object(), model='x', model_sha='x'))['attempt']['status'] == 'proposal'
    bad = deepcopy(receipt); bad['sources'][0]['content'] = 'forged'; bad['receipt_sha256'] = adapter._digest({k:v for k,v in bad.items() if k != 'receipt_sha256'})
    with pytest.raises(ValueError): await adapter.generate(scope, bad, object(), model='x', model_sha='x')
    (folder / 'policy.md').unlink(); await scan_collection('w', scope.collection_ids[0])
    with pytest.raises(ValueError): await adapter.generate(scope, receipt, object(), model='x', model_sha='x')


@pytest.mark.asyncio
async def test_changes_during_generation_discard_result(indexed, monkeypatch):
    scope, folder = indexed; receipt = await adapter.prepare(scope, 'deployment approval')
    async def fake(*args, **kwargs):
        (folder / 'policy.md').write_text('# Policy\n\nThe deployment no longer requires approval.\n')
        await scan_collection('w', scope.collection_ids[0]); return {'status': 'proposal'}
    monkeypatch.setattr(workflow, 'generate', fake)
    with pytest.raises(ValueError): await adapter.generate(scope, receipt, object(), model='x', model_sha='x')


@pytest.mark.asyncio
async def test_stale_scope_and_configuration_change(indexed):
    scope, _ = indexed
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('UPDATE source_collections SET last_scan_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(), scope.collection_ids[0])); await db.commit()
    with pytest.raises(ValueError, match='ready and recent'): await adapter.prepare(scope, 'deployment')
    with pytest.raises(ValueError): await adapter.prepare(EvidenceScope('other', scope.collection_ids), 'deployment')


@pytest.mark.asyncio
async def test_result_limit_is_reported(indexed, monkeypatch):
    scope, folder = indexed
    for i in range(12): (folder / f'{i}.md').write_text(f'# Deployment {i}\n\nDeployment approval is required.\n')
    await scan_collection('w', scope.collection_ids[0])
    r = await adapter.prepare(scope, 'deployment approval')
    assert r['retrieval']['truncated'] and r['retrieval']['trace']['result_limit_reached']
    assert len(r['sources']) == adapter.MAX_RESULTS


@pytest.mark.asyncio
async def test_configuration_mutation_invalidates_receipt(indexed, monkeypatch):
    scope, _ = indexed; receipt = await adapter.prepare(scope, 'deployment approval')
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await db.execute('UPDATE source_collections SET excludes=? WHERE id=?', (json.dumps(['secret/**']), scope.collection_ids[0])); await db.commit()
    async def forbidden(*args, **kwargs): raise AssertionError('generation must not run')
    monkeypatch.setattr(workflow, 'generate', forbidden)
    with pytest.raises(ValueError): await adapter.generate(scope, receipt, object(), model='x', model_sha='x')


@pytest.mark.asyncio
async def test_snapshot_change_during_lookup_is_rejected(indexed, monkeypatch):
    scope, folder = indexed; real = adapter.lookup_evidence
    async def racing(*args, **kwargs):
        result = await real(*args, **kwargs)
        (folder / 'policy.md').write_text('# Policy\n\nDeployment changed.\n')
        await scan_collection('w', scope.collection_ids[0]); return result
    monkeypatch.setattr(adapter, 'lookup_evidence', racing)
    with pytest.raises(ValueError, match='snapshot changed'): await adapter.prepare(scope, 'deployment')


@pytest.mark.asyncio
async def test_no_matching_excerpts_is_explicit(indexed):
    scope, _ = indexed
    with pytest.raises(ValueError, match='No source excerpts'):
        await adapter.prepare(scope, 'unmatchablezz')


@pytest.mark.asyncio
async def test_oversized_source_is_not_clipped(indexed):
    scope, folder = indexed
    (folder / 'policy.md').write_text('# Policy\n\n' + ('Deployment approval. ' * 900))
    await scan_collection('w', scope.collection_ids[0])
    with pytest.raises(ValueError, match='No retrieved whole excerpts fit'):
        await adapter.prepare(scope, 'deployment approval')


@pytest.mark.asyncio
async def test_privacy_disabled_rejects_before_retrieval(indexed, monkeypatch):
    scope, _ = indexed
    from potluck_local.services import investigations
    monkeypatch.setattr(adapter, '_privacy', investigations._privacy)
    monkeypatch.setattr(investigations, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=False), memory=SimpleNamespace(enabled=True)))
    with pytest.raises(PermissionError):
        await adapter.prepare(scope, 'deployment')


@pytest.mark.asyncio
async def test_result_has_selected_excerpt_scope_and_provenance(indexed, monkeypatch):
    scope, _ = indexed
    receipt = await adapter.prepare(scope, 'deployment approval')
    async def fake(*args, **kwargs):
        return {'status': 'proposal', 'result': {'verdict': 'not_stated'}}
    monkeypatch.setattr(workflow, 'generate', fake)
    result = await adapter.generate(scope, receipt, object(), model='x', model_sha='x')
    provenance = result['provenance']
    assert receipt['envelope']['scope'] == adapter.SCOPE
    assert receipt['envelope']['single_focus'] is True
    assert provenance['receipt_sha256'] == receipt['receipt_sha256']
    assert provenance['snapshot_sha256'] == adapter._digest(receipt['snapshot'])
    assert provenance['answer_scope'] == adapter.SCOPE
    assert provenance['not_stated_wording'] == 'Not stated in the selected retrieved excerpts.'
    assert provenance['semantic_collection_search_verified'] is False
    assert provenance['retrieval'] == receipt['retrieval']


@pytest.mark.asyncio
async def test_snapshot_document_limit_rejects_without_partial_receipt(indexed, monkeypatch):
    scope, folder = indexed
    (folder / 'second.md').write_text('# Deployment\n\nDeployment approval.\n')
    await scan_collection('w', scope.collection_ids[0])
    monkeypatch.setattr(adapter, 'MAX_DOCUMENTS', 1)
    with pytest.raises(ValueError, match='document limit'):
        await adapter.prepare(scope, 'deployment')


@pytest.mark.asyncio
async def test_oversized_higher_ranked_chunk_is_reported_and_later_chunk_backfills(indexed, monkeypatch):
    scope, folder = indexed
    (folder / 'policy.md').write_text('# Deployment approval\n\n' + ('Deployment approval. ' * 900))
    (folder / 'later.md').write_text('# Deployment\n\nDeployment requires a review.\n')
    await scan_collection('w', scope.collection_ids[0])
    receipt = await adapter.prepare(scope, 'deployment approval')
    assert len(receipt['ranked_candidates']) == 2
    assert receipt['packing']['candidate_count'] == 2
    assert receipt['packing']['selected_count'] == 1
    omitted = receipt['packing']['omitted']
    assert omitted == [{'citation_id': receipt['ranked_candidates'][0]['citation_id'],
                        'reason': 'workflow_preparation_limit_or_unsupported'}]
    assert receipt['sources'][0]['citation_id'] == receipt['ranked_candidates'][1]['citation_id']
    assert receipt['sources'][0]['content'] == receipt['envelope']['sources'][0]['quote']
    async def fake(*args, **kwargs):
        return {'status': 'proposal', 'result': {'verdict': 'not_stated'}}
    monkeypatch.setattr(workflow, 'generate', fake)
    result = await adapter.generate(scope, receipt, object(), model='x', model_sha='x')
    assert result['provenance']['packing']['omitted'] == omitted
    assert result['provenance']['semantic_collection_search_verified'] is False
    assert result['provenance']['not_stated_wording'] == 'Not stated in the selected retrieved excerpts.'
