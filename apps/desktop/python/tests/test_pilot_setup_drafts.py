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


def test_direct_document_draft_is_scoped_complete_and_read_only(client, tmp_path):
    cid, folder = register(client, tmp_path, {'release.md': '# Draft 2026-01-01\n\nApproval required.\n\n## Exception\n\nOnly during an incident, record next day.'})
    other, _ = register(client, tmp_path, {'release.md': '# Other workspace\n\nDifferent rule.'}, name='other', workspace='other')
    path = '/company-brain/pilot/source-draft'
    body = {'workspace': 'w', 'collection_ids': [cid], 'collection_id': cid, 'relative_path': 'release.md'}
    response = client.post(path, json=body)
    assert response.status_code == 200
    draft = response.json()
    assert draft['collection_id'] == cid and draft['relative_path'] == 'release.md'
    assert draft['document_id'] and draft['revision_id'] and draft['notice'] == ''
    preview = draft['preview']
    assert 'Draft 2026-01-01' in preview['content']
    assert 'Only during an incident, record next day.' in preview['content']
    assert preview['documents'][0]['revision_id'] == draft['revision_id']
    with sqlite3.connect(tmp_path / 'db') as db:
        assert len(preview['citation_ids']) == db.execute('select count(*) from source_chunks where document_id=?', (draft['document_id'],)).fetchone()[0]
        assert db.execute('select count(*) from reviewed_claims').fetchone()[0] == 0
        assert db.execute('select count(*) from investigations').fetchone()[0] == 0
    assert client.post(path, json={**body, 'collection_id': other}).status_code == 400
    assert client.post(path, json={**body, 'workspace': 'other'}).status_code == 400
    assert client.post(path, json={**body, 'relative_path': '../release.md'}).status_code == 400
    assert client.post(path, json={**body, 'relative_path': 'missing.md'}).status_code == 400
    assert client.post(path, json={**body, 'relative_path': 1}).status_code == 422
    (folder / 'release.md').unlink()
    assert client.post(f'/company-brain/pilot/collections/{cid}/scan', json={'workspace': 'w', 'scan_confirmed': True}).status_code == 200
    assert client.post(path, json=body).status_code == 400


def test_direct_document_draft_rejects_any_stale_scope_and_never_truncates(client, tmp_path):
    cid, _ = register(client, tmp_path, {'large.md': '# Release\n\n' + '\n\n'.join('Rule ' + str(i) + 'x' * 1000 for i in range(20))})
    other, _ = register(client, tmp_path, {'small.md': '# Small\n\nText.'}, name='second')
    path = '/company-brain/pilot/source-draft'
    body = {'workspace': 'w', 'collection_ids': [cid, other], 'collection_id': cid, 'relative_path': 'large.md'}
    result = client.post(path, json=body)
    assert result.status_code == 200
    assert result.json()['preview'] is None and 'exceeds' in result.json()['notice']
    with sqlite3.connect(tmp_path / 'db') as db:
        db.execute("update source_collections set last_scan_at='2000-01-01T00:00:00+00:00' where id=?", (other,))
    assert client.post(path, json=body).status_code == 400


def test_direct_document_draft_requires_auth_and_privacy(client, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from potluck_local.app import create_app
    from potluck_local.routers import company_brain_pilot as pilot
    from potluck_local.services import investigations

    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\nRule.'})
    body = {'workspace': 'w', 'collection_ids': [cid], 'collection_id': cid, 'relative_path': 'release.md'}
    path = '/company-brain/pilot/source-draft'
    unauthenticated = TestClient(create_app(), client=('127.0.0.1', 50001))
    assert unauthenticated.post(path, json=body).status_code == 401
    monkeypatch.setattr(pilot, '_privacy', investigations._privacy)
    monkeypatch.setattr(investigations, 'get_settings', lambda: SimpleNamespace(
        privacy=SimpleNamespace(memory_enabled=False), memory=SimpleNamespace(enabled=True)))
    assert client.post(path, json=body).status_code == 403


def test_direct_document_draft_rejects_revision_changed_after_preview(client, tmp_path, monkeypatch):
    from potluck_local.services import claim_source_context, source_collections

    cid, folder = register(client, tmp_path, {'release.md': '# Release\n\nOriginal rule.'})
    original_preview = claim_source_context.preview

    async def preview_then_rescan(scope, citations):
        result = await original_preview(scope, citations)
        (folder / 'release.md').write_text('# Release\n\nChanged rule.')
        await source_collections.scan_collection('w', cid)
        return result

    monkeypatch.setattr(claim_source_context, 'preview', preview_then_rescan)
    body = {'workspace': 'w', 'collection_ids': [cid], 'collection_id': cid, 'relative_path': 'release.md'}
    assert client.post('/company-brain/pilot/source-draft', json=body).status_code == 400


def test_topic_suggestions_remove_metadata_and_link_noise_without_changing_previews(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'manager.md': '---\ntype: task\nstatus: open\n---\n\n# Premium account manager\n\nRemove the account manager promise or staff it.\n\n## Exception\n\nOnly after owner approval, retain the exception.\n\nRelated: [[Worker]]',
        'standalone.md': '---\ntype: task\n---\n\n# Standalone accounts\n\nKey the standalone account by tenant.',
        'worker.md': '---\ntype: task\n---\n\n# Worker\n\nBound the job.\n\nRelated: [[Premium account manager]]',
    })
    q = 'What options does the account manager task give for fixing the promise?'
    legacy = suggest(client, cid, question=q).json()
    assert len(legacy['candidates']) == 3
    assert suggest(client, cid, question=q, suggestion_version='legacy').json() == legacy
    new = suggest(client, cid, question=q, suggestion_version='document-topics-v1').json()
    assert new['version'] == 'pilot-setup-drafts-v2'
    assert [d['relative_path'] for d in new['candidates']] == ['manager.md']
    candidate = new['candidates'][0]
    old = next(c for c in legacy['candidates'] if c['relative_path'] == 'manager.md')
    assert candidate['preview'] == old['preview']
    assert 'retain the exception' in candidate['preview']['content']
    assert 'status: open' in candidate['preview']['content']
    assert 'Related: [[Worker]]' in candidate['preview']['content']
    assert 'manager' in candidate['match']['title_terms']
    assert 'task' in new['query_trace']['ignored_generic_terms']
    assert new['question_parts'] == legacy['question_parts']
    with sqlite3.connect(tmp_path / 'db') as db:
        assert db.execute('select count(*) from reviewed_claims').fetchone()[0] == 0
        assert db.execute('select count(*) from investigations').fetchone()[0] == 0


def test_topic_multi_document_question_and_body_only_matches(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'refunds.md': '# Refund policy\n\nRefunds need a receipt. The adjustment uses a token bucket.',
        'cancellations.md': '# Cancellation policy\n\nCancel the subscription before renewal.',
        'worker.md': '# Worker policy\n\nBackground work uses a queue.',
    })
    new = suggest(client, cid, question='What refund and cancellation policies apply?',
                  suggestion_version='document-topics-v1').json()
    assert {d['relative_path'] for d in new['candidates']} == {'refunds.md', 'cancellations.md'}
    body = suggest(client, cid, question='Where is the token bucket?',
                   suggestion_version='document-topics-v1').json()
    assert [d['relative_path'] for d in body['candidates']] == ['refunds.md']
    assert body['candidates'][0]['match']['text_terms'] == ['bucket', 'token']


@pytest.mark.parametrize('question', ['What does the task say about lunar escrow?', 'Task?', 'Billboard?'])
def test_topic_negative_and_generic_queries_never_fill_unrelated_cards(client, tmp_path, question):
    cid, _ = register(client, tmp_path, {'billing.md': '---\nowner: lunar\ntype: task\n---\n\n# Billing\n\nCancel the bill.\n\nRelated: [[Lunar escrow]]'})
    result = suggest(client, cid, question=question, suggestion_version='document-topics-v1')
    assert result.status_code == 200
    assert result.json()['candidates'] == []
    assert any('do not establish absence' in n for n in result.json()['limitations'])


def test_topic_word_normalization_keeps_identifiers_and_unicode_inspectable(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'CASE-42.md': '# Café customers\n\nCustomer policies differ for café orders.',
        'CASE-43.md': '# News analysis\n\nNews must remain distinct from new.',
    })
    result = suggest(client, cid, question='Which customer policies apply to ＣＡＦÉ?',
                     suggestion_version='document-topics-v1').json()
    assert [d['relative_path'] for d in result['candidates']] == ['CASE-42.md']
    assert 'café' in result['candidates'][0]['match']['title_terms']
    assert suggest(client, cid, question='CASE-99?', suggestion_version='document-topics-v1').json()['candidates'] == []
    exact = suggest(client, cid, question='CASE-42?', suggestion_version='document-topics-v1').json()
    assert [d['relative_path'] for d in exact['candidates']] == ['CASE-42.md']


def test_topic_caps_are_visible_and_never_shorten_the_approval_preview(client, tmp_path, monkeypatch):
    import asyncio
    from potluck_local.services.evidence_lookup import EvidenceScope
    from potluck_local.services import source_suggestions
    cid, _ = register(client, tmp_path, {
        'a.md': '# Release\n\nRelease rule.\n\n' + 'Background. ' * 100 + '\n\nExcept during an incident, notify the owner.',
        'z.md': '# Orbit\n\nOrbit policy.',
    })
    monkeypatch.setattr(source_suggestions, 'MAX_DOCUMENTS', 1)
    monkeypatch.setattr(source_suggestions, 'MAX_FEATURE_CHARS', 200)
    result = suggest(client, cid, question='Release?', suggestion_version='document-topics-v1').json()
    assert result['truncated'] and result['documents_considered'] == 1
    assert 'notify the owner' in result['candidates'][0]['preview']['content']
    assert any('bounded' in n for n in result['limitations'])
    missing = suggest(client, cid, question='Orbit?', suggestion_version='document-topics-v1').json()
    assert missing['candidates'] == [] and missing['truncated']
    measured = asyncio.run(source_suggestions.rank(EvidenceScope('w', (cid,)), 'Release'))
    assert measured['maximum_document_feature_chars_read'] <= 200


def test_topic_scope_auth_version_and_freshness_fail_closed(client, tmp_path):
    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\nOwner review required.'})
    other, _ = register(client, tmp_path, {'secret.md': '# Release\n\nOther workspace rule.'}, name='other', workspace='other')
    assert suggest(client, cid, suggestion_version='future').status_code == 422
    assert suggest(client, cid, workspace='other', suggestion_version='document-topics-v1').status_code == 400
    assert suggest(client, cid, collection_ids=[cid, other], suggestion_version='document-topics-v1').status_code == 400
    body = {'workspace': 'w', 'collection_ids': [cid], 'question': 'Release?', 'suggestion_version': 'document-topics-v1'}
    assert client.post('/company-brain/pilot/setup-drafts', headers={'Authorization': 'Bearer wrong'}, json=body).status_code == 401
    with sqlite3.connect(tmp_path / 'db') as db:
        db.execute("update source_collections set last_scan_at='2000-01-01T00:00:00+00:00'")
    assert suggest(client, cid, suggestion_version='document-topics-v1').status_code == 400


@pytest.mark.parametrize('stage', ['ranking', 'preview'])
def test_topic_revision_changes_are_rejected_before_suggestions_return(client, tmp_path, monkeypatch, stage):
    from potluck_local.services import source_suggestions, source_collections
    cid, folder = register(client, tmp_path, {'release.md': '# Release\n\nOwner review required.'})
    owner = source_suggestions if stage == 'ranking' else drafts
    name = 'rank' if stage == 'ranking' else 'document_draft'
    original = getattr(owner, name)

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        (folder / 'release.md').write_text('# Release\n\nRelease paused.')
        await source_collections.scan_collection('w', cid)
        return result

    monkeypatch.setattr(owner, name, changed)
    assert suggest(client, cid, suggestion_version='document-topics-v1').status_code == 400


def test_topic_feature_cleanup_keeps_malformed_frontmatter_and_related_rules_searchable():
    from potluck_local.services.source_suggestions import _body
    malformed = '---\nstatus: unknown\n\nSpecial condition.'
    assert _body(malformed) == malformed
    rule = 'Related: notify the owner before an exception.\n'
    assert _body(rule) == rule


def test_topic_rank_rejects_scan_of_an_unselected_document_even_when_draft_is_unchanged(client, tmp_path, monkeypatch):
    from potluck_local.services import source_suggestions, source_collections
    cid, folder = register(client, tmp_path, {
        'release.md': '# Release\n\nOwner review required.',
        'other.md': '# Worker\n\nBound the job.',
    })
    original = drafts.document_draft

    async def scan_other(*args, **kwargs):
        result = await original(*args, **kwargs)
        (folder / 'other.md').write_text('# Worker\n\nRelease is a new topic here.')
        await source_collections.scan_collection('w', cid)
        return result

    monkeypatch.setattr(drafts, 'document_draft', scan_other)
    assert suggest(client, cid, question='Release?', suggestion_version='document-topics-v1').status_code == 400


def test_selective_topics_do_not_fill_cards_with_redundant_weak_matches(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'release.md': '# Release backend\n\nUpdate the extension workflow and backend together.',
        'notes.md': '# Backend notes\n\nBackend update notes.',
        'worker.md': '# Worker\n\nQueue job.',
    })
    q = 'Which release workflow updates the extension backend?'
    v1 = suggest(client, cid, question=q, suggestion_version='document-topics-v1').json()
    assert len(v1['candidates']) > 1
    v2 = suggest(client, cid, question=q, suggestion_version='document-topics-v2').json()
    assert [c['relative_path'] for c in v2['candidates']] == ['release.md']
    assert v2['candidates'][0]['preview'] == v1['candidates'][0]['preview']


def test_selective_topics_keep_competing_strong_sources_with_identical_topic_words(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'old.md': '# Release approval\n\nOwner approval required.',
        'new.md': '# Release approval\n\nLead approval required instead.',
        'worker.md': '# Worker\n\nQueue job.',
    })
    v2 = suggest(client, cid, question='Who gives release approval?', suggestion_version='document-topics-v2').json()
    assert {c['relative_path'] for c in v2['candidates']} == {'old.md', 'new.md'}


def test_selective_topics_keep_distinct_requested_topics(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'refund.md': '# Refund policy\n\nRefunds need a receipt.',
        'cancel.md': '# Cancellation policy\n\nCancellation is recorded before renewal.',
        'worker.md': '# Worker policy\n\nQueue job.',
    })
    v2 = suggest(client, cid, question='What refund and cancellation policies apply?', suggestion_version='document-topics-v2').json()
    assert {c['relative_path'] for c in v2['candidates']} == {'refund.md', 'cancel.md'}


def test_selective_topics_decline_one_word_of_an_unestablished_topic(client, tmp_path):
    cid, _ = register(client, tmp_path, {'release.md': '# Release\n\nLaunch the web app.'})
    q = 'Which task handles orbital launch telemetry?'
    assert suggest(client, cid, question=q, suggestion_version='document-topics-v1').json()['candidates']
    assert suggest(client, cid, question=q, suggestion_version='document-topics-v2').json()['candidates'] == []


def test_clause_topics_do_not_mistake_a_background_reference_for_the_question_subject(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'input.md': '# Product input\n\nA visitor can override back-in-stock email product titles.',
        'retention.md': '# Email records\n\nRetain back-in-stock email rows.',
        'worker.md': '# Worker\n\nQueue job.',
    })
    result = suggest(client, cid, question='How can a visitor influence a back-in-stock email?',
                     suggestion_version='document-topics-v3').json()
    assert [c['relative_path'] for c in result['candidates']] == ['input.md']


def test_clause_topics_preserve_different_requests_and_competing_strong_rules(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'refund.md': '# Refund policy\n\nRefunds need a receipt.',
        'cancel.md': '# Cancellation policy\n\nCancellation before renewal.',
        'old.md': '# Release approval\n\nOwner approval required.',
        'new.md': '# Release approval\n\nLead approval required instead.',
    })
    both = suggest(client, cid, question='What refund policy applies and when is cancellation allowed?',
                   suggestion_version='document-topics-v3').json()
    assert {c['relative_path'] for c in both['candidates']} == {'refund.md', 'cancel.md'}
    competing = suggest(client, cid, question='Who gives release approval?',
                        suggestion_version='document-topics-v3').json()
    assert {c['relative_path'] for c in competing['candidates']} == {'old.md', 'new.md'}


def test_display_topics_support_explicit_language_names_without_promoting_file_extensions():
    from potluck_local.services.source_suggestions import _words, _body
    assert _words('JS JavaScript ＪＳ', abbreviations=True) == {'javascript'}
    assert _words('TS TypeScript', abbreviations=True) == {'typescript'}
    assert 'javascript' not in _words('harness.js', abbreviations=True)
    assert _body('Read [[Hidden JavaScript title|the guide]].', display_links=True) == 'Read the guide.'
    assert _body('Read [[Hidden JavaScript title|the guide]].') == 'Read [[Hidden JavaScript title|the guide]].'


def test_display_topics_retrieve_both_budget_and_harness_documents_without_altering_link_previews(client, tmp_path):
    cid, _ = register(client, tmp_path, {
        'budget.md': '# Budget JS\n\nMeasure script size and set the JS size budget.',
        'harness.md': '# Browser harness CI\n\nRun browser harnesses in CI. Move the size budget from [[Budget JS|PERF]].',
        'worker.md': '# Worker\n\nQueue job.',
    })
    result = suggest(client, cid, question='What covers JavaScript size budgets and browser harnesses in CI?',
                     suggestion_version='document-topics-v4').json()
    assert {c['relative_path'] for c in result['candidates']} == {'budget.md', 'harness.md'}
    budget = next(c for c in result['candidates'] if c['relative_path'] == 'budget.md')
    assert 'javascript' in budget['match']['title_terms']
    harness = next(c for c in result['candidates'] if c['relative_path'] == 'harness.md')
    assert '[[Budget JS|PERF]]' in harness['preview']['content']
