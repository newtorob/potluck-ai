import asyncio
import sqlite3

import pytest

from potluck_local.routers import company_brain_pilot as pilot
from potluck_local.services import claim_source_context as context
from potluck_local.services.evidence_lookup import EvidenceScope

RULE = 'A release requires written owner approval before deployment, unless the emergency incident procedure applies. Keep the prerequisite and exception together when reviewing this dated draft.'


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    db = tmp_path / 'db'
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(db))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)

    async def create(name='Policies', workspace='w', opening='DRAFT. Last updated: 2026-01-01.', rule=RULE):
        folder = tmp_path / name
        folder.mkdir()
        file = folder / 'policy.md'
        file.write_text('# Team policy\n\n' + opening + '\n\n## Release\n\n' + rule)
        cid = (await pilot.register(pilot.Register(workspace=workspace, name=name, folder=str(folder), scan_confirmed=True)))['collection_id']
        await pilot.scan(cid, pilot.Scan(workspace=workspace, scan_confirmed=True))
        scope = {'workspace': workspace, 'collection_ids': [cid]}
        found = await pilot.evidence(pilot.Evidence(**scope, query='release'))
        source = next(s for s in found['source_chunks'] if s['heading'] == 'Release')
        return scope, source, file

    return create, db


def test_preview_preserves_opening_conditions_citations_and_separate_approval(prepared):
    create, db = prepared
    async def run():
        scope, source, _ = await create()
        result = await pilot.context_preview(pilot.ContextPreview(**scope, citation_ids=[source['citation_id']]))
        assert 'Team policy\n\nDRAFT. Last updated: 2026-01-01.' in result['content']
        assert RULE in result['content'] and len(result['citation_ids']) == 2
        assert len(result['documents']) == 1
        assert result['documents'][0]['excerpts'][0]['content'] == RULE
        assert result['limitations'] and result['preview_id']
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT COUNT(*) FROM reviewed_claims').fetchone()[0] == 0
        claim = await pilot.propose(pilot.Proposal(**scope, claim_key='release', content=result['content'], citation_ids=result['citation_ids']))
        rows = (await pilot.claims(pilot.Claims(**scope)))['claims']
        assert rows[0]['id'] == claim['claim_id'] and rows[0]['status'] == 'proposed'
        assert len(rows[0]['sources']) == 2 and rows[0]['content'] == result['content']
        assert not (await pilot.claims(pilot.Claims(**scope, status='approved')))['claims']
    asyncio.run(run())


def test_snapshot_is_explicit_and_header_rescan_revokes_preview_citations(prepared):
    create, _ = prepared
    async def run():
        scope, source, file = await create()
        ids = [source['citation_id']]
        first = await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), ids)
        file.write_text(file.read_text().replace('2026-01-01', '2026-02-01'))
        before_scan = await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), ids)
        assert before_scan == first and any('rescan' in s for s in first['limitations'])
        await pilot.scan(scope['collection_ids'][0], pilot.Scan(workspace='w', scan_confirmed=True))
        with pytest.raises(ValueError, match='citation'):
            await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), ids)
        found = await pilot.evidence(pilot.Evidence(**scope, query='release'))
        current = next(s for s in found['source_chunks'] if s['heading'] == 'Release')
        new = await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), [current['citation_id']])
        assert '2026-02-01' in new['content'] and new['preview_id'] != first['preview_id']
    asyncio.run(run())


def test_rejects_invalid_foreign_duplicate_and_stale_references(prepared):
    create, db = prepared
    async def run():
        scope, source, _ = await create()
        _, foreign, _ = await create('Other', workspace='other')
        s = EvidenceScope('w', tuple(scope['collection_ids']))
        for ids in ([], [source['citation_id']] * 2, ['claim:missing'], [foreign['citation_id']], [source['citation_id'] + 'x'], ['source:../bad:revision']):
            with pytest.raises(ValueError): await context.preview(s, ids)
        with pytest.raises(ValueError): await context.preview(EvidenceScope('other', s.collection_ids), [source['citation_id']])
        with sqlite3.connect(db) as conn:conn.execute("UPDATE source_collections SET last_scan_at='2000-01-01T00:00:00+00:00'")
        with pytest.raises(ValueError, match='ready and recent'): await context.preview(s, [source['citation_id']])
    asyncio.run(run())


def test_groups_same_filename_by_collection_and_deduplicates_opening(prepared):
    create, _ = prepared
    async def run():
        first, one, _ = await create('Team A')
        second, two, _ = await create('Team B')
        scope = EvidenceScope('w', tuple(first['collection_ids'] + second['collection_ids']))
        result = await context.preview(scope, [one['citation_id'], two['citation_id']])
        assert len(result['documents']) == 2 and len(result['citation_ids']) == 4
        assert 'Team A / policy.md' in result['content'] and 'Team B / policy.md' in result['content']
        opening = result['documents'][0]['opening'][0]['citation_id']
        same = await context.preview(scope, [opening, one['citation_id']])
        assert len(same['citation_ids']) == 2 and same['content'].count('2026-01-01') == 1
    asyncio.run(run())


@pytest.mark.parametrize('opening,rule', [('x' * 6001, RULE), ('DRAFT', 'x' * 16001)])
def test_oversize_context_or_excerpt_fails_without_partial_output(prepared, opening, rule):
    create, _ = prepared
    async def run():
        scope, source, _ = await create(opening=opening, rule=rule)
        with pytest.raises(ValueError, match='preview limit'):
            await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), [source['citation_id']])
    asyncio.run(run())


def test_expanded_citation_limit_never_drops_openings(prepared):
    create, _ = prepared
    async def run():
        scopes, ids = [], []
        for i in range(9):
            scope, source, _ = await create(str(i));scopes += scope['collection_ids'];ids.append(source['citation_id'])
        with pytest.raises(ValueError, match='preview limit'):
            await context.preview(EvidenceScope('w', tuple(scopes)), ids)
    asyncio.run(run())


def test_preview_uses_redacted_indexed_text_and_never_invents_date_status(prepared):
    create, _ = prepared
    async def run():
        secret = 'sk-' + 'a' * 48
        scope, source, _ = await create(opening='An undated note. api_key=' + secret)
        result = await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), [source['citation_id']])
        assert secret not in result['content'] and 'An undated note.' in result['content']
        assert '2026-' not in result['content'] and 'approved' not in result['content']
    asyncio.run(run())


def test_context_http_auth_validation_and_privacy(prepared, monkeypatch):
    from fastapi.testclient import TestClient
    from potluck_local.app import create_app
    create, _ = prepared
    scope, source, _ = asyncio.run(create())
    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN', 'test-only')
    client = TestClient(create_app(), client=('127.0.0.1', 53951))
    path = '/company-brain/pilot/claim-context';body = {**scope, 'citation_ids': [source['citation_id']]}
    assert client.post(path, json=body).status_code == 401
    headers = {'Authorization': 'Bearer test-only'}
    assert client.post(path, json=body, headers=headers).status_code == 200
    for value in ([], [42], ['x'] * 17):
        assert client.post(path, json={**body, 'citation_ids': value}, headers=headers).status_code == 422
    assert client.post(path, json={**body, 'extra': True}, headers=headers).status_code == 422
    def denied(): raise PermissionError('Memory is disabled')
    monkeypatch.setattr(pilot, '_privacy', denied)
    assert client.post(path, json=body, headers=headers).status_code == 403


def test_untitled_leading_text_and_first_named_section_remain_in_opening(prepared):
    create, _ = prepared
    async def run():
        scope, source, file = await create()
        file.write_text('---\ndate: 2026-01-01\nstatus: draft\n' + 'notes: not approved; ' * 12 + '\n---\n\n# Team policy\n\n' + 'Review this historical document carefully. ' * 8 + '\n\n## Release\n\n' + RULE)
        await pilot.scan(scope['collection_ids'][0], pilot.Scan(workspace='w', scan_confirmed=True))
        found = await pilot.evidence(pilot.Evidence(**scope, query='release'))
        selected = next(s for s in found['source_chunks'] if s['heading'] == 'Release')
        result = await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), [selected['citation_id']])
        assert len(result['documents'][0]['opening']) == 2
        assert 'status: draft' in result['content'] and 'date: 2026-01-01' in result['content']
        assert 'Team policy' in result['content'] and RULE in result['content']
    asyncio.run(run())


def test_draft_label_in_fifth_opening_chunk_is_not_silently_omitted(prepared):
    create, _ = prepared
    async def run():
        paragraphs = ['p' * 1001] * 4 + ['DRAFT. Last updated: 2026-01-01. ' + 'q' * 970]
        scope, source, _ = await create(opening='\n\n'.join(paragraphs))
        with pytest.raises(ValueError, match='preview limit'):
            await context.preview(EvidenceScope('w', tuple(scope['collection_ids'])), [source['citation_id']])
    asyncio.run(run())
