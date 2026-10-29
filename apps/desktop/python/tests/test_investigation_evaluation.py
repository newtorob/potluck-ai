import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from potluck_local.services import investigation_evaluation as evaluation
from potluck_local.services.evidence_lookup import EvidenceScope


def test_frozen_holdout_hash():
    root = Path(__file__).parents[1] / 'evals' / 'company_brain'
    assert hashlib.sha256((root / 'holdout.json').read_bytes()).hexdigest() == (
        root / 'holdout.sha256').read_text().split()[0]


def run_for(manifest):
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                       ensure_ascii=False).encode()).hexdigest()
    return {'corpus_id': manifest['corpus_id'], 'manifest_sha256': digest,
            'case_ids': [c['id'] for c in manifest['cases']],
            'model_config': {'model_sha256': 'a' * 64, 'settings': {}, 'runtime_version': 'test'}, 'outputs': {}}


def test_comparison_counts_missing_and_separates_labels():
    manifest = {'corpus_id': 'frozen', 'cases': [{'id': 'a'}, {'id': 'b'}]}
    baseline, candidate = run_for(manifest), run_for(manifest)
    baseline['outputs']['a'] = {'citation_validity': True, 'semantic_support': False,
                                'unsupported_answer': True, 'correction': True,
                                'latency_seconds': 2}
    report = evaluation.compare_evaluations(manifest, baseline, candidate)
    assert report['baseline']['failed_cases'] == ['a', 'b']
    assert report['baseline']['rates']['citation_validity'] == 1
    assert report['baseline']['rates']['semantic_support'] == 0
    assert report['baseline']['mean_seconds']['time_to_accepted_seconds'] is None
    assert report['candidate']['failed_cases'] == ['a', 'b']
    candidate['manifest_sha256'] = 'wrong'
    with pytest.raises(ValueError):
        evaluation.compare_evaluations(manifest, baseline, candidate)


@pytest.mark.asyncio
async def test_proposal_is_reference_only_and_revalidated(monkeypatch):
    scope = EvidenceScope('work', ('collection',))
    item = {'status': 'reviewed', 'review': {'decision': 'correct', 'reason': 'private'},
            'outcome': {'result': 'resolved', 'notes': 'private'},
            'evidence': [{'citation_id': 'cite', 'content': 'private source'}],
            'model_slug': 'local', 'model_sha': 'a' * 64}
    get = AsyncMock(return_value=item)
    resolve = AsyncMock(return_value={'content': 'private source'})
    monkeypatch.setattr(evaluation, 'get_investigation', get)
    monkeypatch.setattr(evaluation, 'resolve_citation', resolve)
    with pytest.raises(PermissionError):
        await evaluation.propose_evaluation_case(scope, 'id', owner_confirmed=False)
    proposal = await evaluation.propose_evaluation_case(scope, 'id', owner_confirmed=True)
    assert 'private' not in str(proposal)
    assert proposal['review_decision'] == 'correct' and proposal['outcome_result'] == 'resolved'
    assert await evaluation.validate_evaluation_case(scope, proposal)
    resolve.return_value = None
    assert not await evaluation.validate_evaluation_case(scope, proposal)
    item['outcome'] = None
    with pytest.raises(ValueError):
        await evaluation.propose_evaluation_case(scope, 'id', owner_confirmed=True)


@pytest.mark.asyncio
async def test_source_case_comparison_revalidates(monkeypatch):
    manifest = {'corpus_id': 'x', 'cases': [{'id': 'a', 'proposal': {'investigation_id': 'id'}}]}
    run = run_for(manifest)
    valid = AsyncMock(return_value=False)
    monkeypatch.setattr(evaluation, 'validate_evaluation_case', valid)
    with pytest.raises(ValueError, match='unavailable'):
        await evaluation.compare_reviewed_evaluations(EvidenceScope('w', ('c',)), manifest, run, run)
    valid.return_value = True
    result = await evaluation.compare_reviewed_evaluations(
        EvidenceScope('w', ('c',)), manifest, run, run)
    assert result['candidate']['missing_outputs'] == ['a']


@pytest.mark.asyncio
@pytest.mark.parametrize('remove', [False, True])
async def test_real_review_proposal_invalidation(tmp_path, monkeypatch, remove):
    from types import SimpleNamespace
    import aiosqlite
    from potluck_local.services import investigations as inv
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]['content'])
            citation = payload['evidence'][0]['citation_id']
            brief = {'facts': [{'text': 'Release changed', 'citation_ids': [citation]}],
                     'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [],
                     'missing_evidence': []}
            return {'choices': [{'message': {'content': json.dumps(brief)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    await init_memory_db()
    folder = tmp_path / 'docs'
    folder.mkdir()
    document = folder / 'release.md'
    document.write_text('# Release\nRelease secret-source-marker changed.')
    cid = await register_collection('w', 'docs', folder)
    await scan_collection('w', cid)
    scope = EvidenceScope('w', (cid,))
    item = await inv.create_investigation(scope, 'Release issue', 'release', 'model')
    await inv.review_investigation(scope, item['id'], 'accept', 'owner', 'reviewed')
    await inv.record_outcome(scope, item['id'], 'owner', 'resolved', 'done')
    case = await evaluation.propose_evaluation_case(scope, item['id'], owner_confirmed=True)
    assert 'secret-source-marker' not in json.dumps(case)
    citation = item['brief']['facts'][0]['citation_ids'][0]
    claim = await evaluation.propose_knowledge_update(
        scope, item['id'], 'release', 'Release changed', [citation], owner_confirmed=True)
    async with aiosqlite.connect(get_memory_db_path()) as db:
        row = await (await db.execute('SELECT status FROM reviewed_claims WHERE id=?', (claim,))).fetchone()
        assert row == ('proposed',)
        origin = await (await db.execute(
            'SELECT investigation_id FROM investigation_claim_origins WHERE claim_id=?',
            (claim,))).fetchone()
        assert origin == (item['id'],)
    assert await evaluation.validate_evaluation_case(scope, case)
    if remove:
        document.unlink()
    else:
        document.write_text('# Release\nRelease updated.')
    await scan_collection('w', cid)
    assert not await evaluation.validate_evaluation_case(scope, case)
    async with aiosqlite.connect(get_memory_db_path()) as db:
        row = await (await db.execute(
            'SELECT status, content FROM reviewed_claims WHERE id=?', (claim,))).fetchone()
        assert row == ('deleted', '')


def test_synthetic_inputs_frozen_and_invalid_model_rejected():
    root = Path(__file__).parents[1] / 'evals' / 'company_brain'
    assert hashlib.sha256((root / 'synthetic_cases.json').read_bytes()).hexdigest() == (
        root / 'synthetic_cases.sha256').read_text().split()[0]
    manifest = json.loads((root / 'synthetic_cases.json').read_text())
    baseline, candidate = run_for(manifest), run_for(manifest)
    candidate['model_config']['model_sha256'] = 'z' * 64
    with pytest.raises(ValueError, match='Pinned model'):
        evaluation.compare_evaluations(manifest, baseline, candidate)
    result = evaluation.compare_evaluations(manifest, baseline, baseline)
    assert result['baseline']['all_case_pass_rate'] == 0


@pytest.mark.asyncio
async def test_reviewed_comparison_rejects_empty_manifest():
    with pytest.raises(ValueError):
        await evaluation.compare_reviewed_evaluations(
            EvidenceScope('w', ('c',)), {'corpus_id': 'x', 'cases': []}, {}, {})


def test_comparison_rejects_malformed_artifacts_safely():
    with pytest.raises(ValueError, match='Invalid evaluation artifacts'):
        evaluation.compare_evaluations(None, {}, {})
    with pytest.raises(ValueError):
        evaluation.compare_evaluations({'corpus_id': 'x', 'cases': [{'id': 'a'}]},
                                       {'case_ids': None}, {})


@pytest.mark.asyncio
async def test_scope_zero_hit_invalidation_scrubs_origin_claim(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import aiosqlite
    from potluck_local.services import investigations as inv
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            citation = json.loads(messages[1]['content'])['evidence'][0]['citation_id']
            brief = {'facts': [{'text': 'Alpha', 'citation_ids': [citation]}], 'prior_fixes': [],
                     'hypotheses': [], 'recommended_checks': [], 'missing_evidence': []}
            return {'choices': [{'message': {'content': json.dumps(brief)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    await init_memory_db()
    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir(); two.mkdir()
    (one / 'a.md').write_text('# Alpha\nAlpha exists.')
    (two / 'b.md').write_text('# Other\nNothing relevant.')
    first = await register_collection('w', 'one', one)
    second = await register_collection('w', 'two', two)
    await scan_collection('w', first); await scan_collection('w', second)
    scope = EvidenceScope('w', (first, second))
    item = await inv.create_investigation(scope, 'Alpha issue', 'alpha', 'model')
    assert all(e['collection_id'] == first for e in item['evidence'])
    await inv.review_investigation(scope, item['id'], 'accept', 'owner', 'reviewed')
    await inv.record_outcome(scope, item['id'], 'owner', 'resolved', 'done')
    citation = item['brief']['facts'][0]['citation_ids'][0]
    claim = await evaluation.propose_knowledge_update(
        scope, item['id'], 'alpha', 'Alpha claim', [citation], owner_confirmed=True)
    (two / 'new.md').write_text('# Alpha\nAlpha contradicts prior evidence.')
    await scan_collection('w', second)
    async with aiosqlite.connect(get_memory_db_path()) as db:
        row = await (await db.execute('SELECT status,content FROM reviewed_claims WHERE id=?',
                                      (claim,))).fetchone()
        assert row == ('deleted', '')
        row = await (await db.execute('SELECT status,brief FROM investigations WHERE id=?',
                                      (item['id'],))).fetchone()
        assert row == ('invalidated', '')


@pytest.mark.asyncio
async def test_derived_claim_chain_is_invalidated_transactionally(tmp_path, monkeypatch):
    import aiosqlite
    from potluck_local.services import investigations as inv
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    await init_memory_db()
    async with aiosqlite.connect(get_memory_db_path()) as db:
        await inv.ensure_schema(db)
        for iid in ('i1', 'i2'):
            await db.execute(
                'INSERT INTO investigations (id,workspace,scope,issue,model_slug,status,brief,'
                'evidence,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (iid, 'w', '["c"]', 'secret', 'm', 'reviewed', 'secret', 'secret', 'now'))
        for claim in ('b', 'c'):
            await db.execute(
                'INSERT INTO reviewed_claims (id,workspace,claim_key,content,status,created_at) '
                'VALUES (?,?,?,?,?,?)', (claim, 'w', claim, 'secret', 'approved', 'now'))
        await db.execute('INSERT INTO investigation_bindings VALUES (?,?,?,?,?,?)',
                         ('i1', 'c', 'd', 'r', 'k', 'a'))
        await db.execute('INSERT INTO investigation_bindings VALUES (?,?,?,?,?,?)',
                         ('i2', 'c', 'd', 'r', 'k', 'b'))
        await db.execute('INSERT INTO investigation_claim_origins VALUES (?,?)', ('b', 'i1'))
        await db.execute('INSERT INTO investigation_claim_origins VALUES (?,?)', ('c', 'i2'))
        await db.commit()
        await db.execute('BEGIN IMMEDIATE')
        await inv.invalidate(db, 'claim_id=?', ('a',))
        investigations = await (await db.execute(
            'SELECT status,issue,brief,evidence FROM investigations ORDER BY id')).fetchall()
        claims = await (await db.execute(
            'SELECT status,content FROM reviewed_claims ORDER BY id')).fetchall()
        assert investigations == [('invalidated', '', '', ''), ('invalidated', '', '', '')]
        assert claims == [('deleted', ''), ('deleted', '')]
        assert len(await (await db.execute('SELECT * FROM investigation_claim_origins')).fetchall()) == 2
        await db.rollback()
        assert (await (await db.execute(
            "SELECT status FROM investigations WHERE id='i2'")).fetchone()) == ('reviewed',)


@pytest.mark.asyncio
async def test_supersede_rejects_replacement_dependent_on_old_claim(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import aiosqlite
    from potluck_local.services import investigations as inv
    from potluck_local.services.evidence_lookup import lookup_evidence
    from potluck_local.services.memory_db import get_memory_db_path, init_memory_db
    from potluck_local.services.reviewed_knowledge import approve_claim, propose_claim, supersede_claim
    from potluck_local.services.source_collections import register_collection, scan_collection

    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setattr(inv, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=True), memory=SimpleNamespace(enabled=True)))
    await init_memory_db()
    folder = tmp_path / 'docs'
    folder.mkdir()
    (folder / 'a.md').write_text('# Alpha\nAlpha deployment is complete.')
    collection = await register_collection('w', 'docs', folder)
    await scan_collection('w', collection)
    scope = EvidenceScope('w', (collection,))
    source = (await lookup_evidence(scope, 'alpha'))['source_chunks'][0]
    ref = {k: source[k] for k in ('collection_id', 'document_id', 'revision_id', 'chunk_id')}
    old = await propose_claim('w', (collection,), 'alpha', 'Alpha deployment is complete.', [ref])
    await approve_claim('w', (collection,), old, 'owner', 'checked')
    claim_citation = (await lookup_evidence(scope, 'alpha'))['reviewed_claims'][0]['citation_id']

    class Runtime:
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'a' * 64
        async def chat(self, messages, **kwargs):
            brief = {'facts': [{'text': 'Alpha complete', 'citation_ids': ['E2']}],
                     'prior_fixes': [], 'hypotheses': [], 'recommended_checks': [],
                     'missing_evidence': []}
            return {'choices': [{'message': {'content': json.dumps(brief)}}]}
    monkeypatch.setattr(inv, 'get_runtime', Runtime)
    item = await inv.create_investigation(scope, 'Alpha', 'alpha', 'model')
    await inv.review_investigation(scope, item['id'], 'accept', 'owner', 'reviewed')
    await inv.record_outcome(scope, item['id'], 'owner', 'resolved', 'done')
    replacement = await evaluation.propose_knowledge_update(
        scope, item['id'], 'alpha', 'Alpha deployment confirmed.', [claim_citation],
        owner_confirmed=True)
    with pytest.raises(ValueError, match='depends on invalidated'):
        await supersede_claim('w', (collection,), old, replacement, 'owner', 'correction')
    async with aiosqlite.connect(get_memory_db_path()) as db:
        rows = await (await db.execute(
            'SELECT id,status,claim_key,content FROM reviewed_claims WHERE id IN (?,?)',
            (old, replacement))).fetchall()
        by_id = {row[0]: row[1:] for row in rows}
        assert by_id[old] == ('approved', 'alpha', 'Alpha deployment is complete.')
        assert by_id[replacement] == ('proposed', 'alpha', 'Alpha deployment confirmed.')
        assert (await (await db.execute('SELECT status FROM investigations WHERE id=?',
                                        (item['id'],))).fetchone()) == ('reviewed',)
