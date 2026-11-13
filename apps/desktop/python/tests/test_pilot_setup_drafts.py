import sqlite3

import pytest
from fastapi.testclient import TestClient

from potluck_local.services import pilot_setup_drafts as drafts


@pytest.fixture
def client(tmp_path, monkeypatch):
    from potluck_local.app import create_app
    from potluck_local.routers import company_brain_pilot as pilot
    monkeypatch.setenv('POTLUCK_SIDECAR_TOKEN', 'setup-test')
    monkeypatch.setenv('POTLUCK_MEMORY_DB_PATH', str(tmp_path / 'db'))
    monkeypatch.setenv('POTLUCK_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(pilot, '_privacy', lambda: None)
    return TestClient(create_app(), headers={'Authorization': 'Bearer setup-test'}, client=('127.0.0.1', 50001))


def register(client, tmp_path, files, name='policies', workspace='w'):
    folder = tmp_path / name
    folder.mkdir()
    for path, text in files.items():
        (folder / path).write_text(text)
    registered = client.post('/company-brain/pilot/collections', json={
        'workspace': workspace, 'name': name, 'folder': str(folder), 'scan_confirmed': True})
    assert registered.status_code == 200
    cid = registered.json()['collection_id']
    assert client.post(f'/company-brain/pilot/collections/{cid}/scan', json={
        'workspace': workspace, 'scan_confirmed': True}).status_code == 200
    return cid, folder


def suggest(client, cid, question='Who approves release? When is the emergency exception allowed?', **extra):
    return client.post('/company-brain/pilot/setup-drafts', json={
        'workspace': 'w', 'collection_ids': [cid], 'question': question, **extra})


def test_literal_parts_preserve_request_and_never_split_conditions_or_quoted_punctuation():
    q = 'According to the draft, who approves release? When may we bypass approval?'
    plan = drafts.question_parts(q)
    assert plan['parts'] == ['According to the draft, who approves release?', 'When may we bypass approval?']
    assert ' '.join(plan['parts']) == q
    for q in ['If A and B apply, can we release and who approves?', 'What does "Ready? When" mean?', "What happens if it's late? Who calls?", ' '.join(['Who approves?'] * 7)]:
        assert drafts.question_parts(q)['parts'] == [q]
    for q in ['', ' ', 'x' * 501, None]:
        with pytest.raises(ValueError):
            drafts.question_parts(q)


@pytest.mark.parametrize('question', [
    'For "Café open? Who approves?", who reviews the draft?',
    'If A and B apply, can we release and who approves?',
])
def test_suggested_literal_request_remains_one_unconfirmed_part_until_explicit_ask(
        client, tmp_path, monkeypatch, question):
    from potluck_local.routers import company_brain_pilot as pilot
    from potluck_local.services import investigation_request_parts

    calls = []

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return {'id': 'reviewed-test'}

    monkeypatch.setattr(pilot, 'create_investigation', capture)
    cid, _ = register(client, tmp_path, {'policy.md': '# Review\n\nApproval requires review.'})
    result = suggest(client, cid, question=question)
    assert result.status_code == 200
    parts = result.json()['question_parts']
    assert parts['question'] == question
    assert parts['parts'] == [question]
    assert parts['coverage'] == 'not_verified'
    assert calls == []
    with sqlite3.connect(tmp_path / 'db') as db:
        assert db.execute('select count(*) from reviewed_claims').fetchone()[0] == 0
        assert db.execute('select count(*) from investigations').fetchone()[0] == 0

    body = {'workspace': 'w', 'collection_ids': [cid], 'question': question,
            'model_slug': 'model', 'pilot_confirmed': True,
            'question_parts': parts['parts'], 'question_parts_confirmed': False}
    path = '/company-brain/pilot/ask-reviewed'
    assert client.post(path, json=body).status_code == 400
    assert calls == []
    assert client.post(path, json={**body, 'question_parts_confirmed': True}).json() == {
        'id': 'reviewed-test'}
    assert len(calls) == 1
    args, options = calls[0]
    assert args[1:3] == (question, question)
    assert options['reviewed_operations'] == [{'request': question, 'operation': 'answer'}]
    assert options['question_part_plan'] == investigation_request_parts.prepare(
        question, [question], origin='reviewer_supplied')


def test_suggestions_include_later_nonmatching_exception_and_make_no_authority_writes(client, tmp_path):
    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\nDRAFT 2026-01-01.\n\n## Rule\n\nRelease requires owner approval.\n\n## Boundary\n\nExcept during a declared incident, record the decision next day.'})
    result = suggest(client, cid)
    assert result.status_code == 200
    body = result.json()
    assert body['question_parts']['coverage'] == 'not_verified'
    assert body['question_parts']['parts'] == ['Who approves release?', 'When is the emergency exception allowed?']
    candidate = body['candidates'][0]
    preview = candidate['preview']
    assert 'DRAFT 2026-01-01' in preview['content']
    assert 'record the decision next day' in preview['content']
    with sqlite3.connect(tmp_path / 'db') as db:
        total = db.execute('select count(*) from source_chunks').fetchone()[0]
    assert len(preview['citation_ids']) == total
    assert client.post('/company-brain/pilot/claims/list', json={'workspace': 'w', 'collection_ids': [cid]}).json()['claims'] == []
    with sqlite3.connect(tmp_path / 'db') as db:
        assert db.execute('select count(*) from investigations').fetchone()[0] == 0


def test_scope_auth_and_freshness_are_required(client, tmp_path):
    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\nRelease requires owner approval.'})
    assert suggest(client, cid, workspace='other').status_code == 400
    assert suggest(client, cid, collection_ids=[cid, cid]).status_code == 400
    assert suggest(client, cid, question='x' * 501).status_code == 422
    assert client.post('/company-brain/pilot/setup-drafts', headers={'Authorization': 'Bearer wrong'}, json={'workspace': 'w', 'collection_ids': [cid], 'question': 'Release?'}).status_code == 401
    with sqlite3.connect(tmp_path / 'db') as db:
        db.execute("update source_collections set last_scan_at='2000-01-01T00:00:00+00:00'")
    assert suggest(client, cid).status_code == 400


def test_absence_is_not_invented_and_oversize_never_becomes_partial_draft(client, tmp_path):
    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\n' + '\n\n'.join('Release rule ' + str(i) + 'x' * 1000 for i in range(20))})
    result = suggest(client, cid).json()
    assert result['candidates'][0]['preview'] is None
    assert 'exceeds' in result['candidates'][0]['notice']
    empty = suggest(client, cid, question='Zyzzyva?').json()
    assert empty['candidates'] == []
    assert empty['limitations']


def test_candidate_limit_and_query_omissions_are_visible(client, tmp_path):
    cid, _ = register(client, tmp_path, {f'{i}.md': f'# Release\n\nRelease rule {i}.' for i in range(5)})
    question = 'Release ' + ' '.join(f'term{i}' for i in range(25)) + '?'
    result = suggest(client, cid, question=question).json()
    assert len(result['candidates']) == 3
    assert result['matching_documents_seen'] == 5
    assert result['truncated']
    assert result['query_trace']['omitted_terms']


def test_source_revision_is_revalidated_by_existing_proposal_route(client, tmp_path):
    cid, folder = register(client, tmp_path, {'release.md': '# Release\n\nRelease requires approval.'})
    preview = suggest(client, cid).json()['candidates'][0]['preview']
    (folder / 'release.md').write_text('# Release\n\nRelease is paused.')
    assert client.post(f'/company-brain/pilot/collections/{cid}/scan', json={'workspace': 'w', 'scan_confirmed': True}).status_code == 200
    rejected = client.post('/company-brain/pilot/claims', json={'workspace': 'w', 'collection_ids': [cid], 'claim_key': 'release', 'content': preview['content'], 'citation_ids': preview['citation_ids']})
    assert rejected.status_code == 400
