import asyncio
from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_revision as revision
from potluck_local.services import investigation_verification, investigations


def source_envelope():
    return {'version': 'draft-verification-v3', 'groups': [
        {'id': 'G1', 'focus': 'Policy', 'request': 'What applies?', 'status': 'supported',
         'draft_sentences': ['Old answer.']},
        {'id': 'G2', 'focus': 'Other', 'request': 'What else?', 'status': 'supported',
         'draft_sentences': ['Other answer.']}],
        'source_checks': [
            {'id': 'Q1', 'group_id': 'G1', 'snippet_id': 'S1', 'quote': 'The rule applies.',
             'context': {'heading': 'Policy', 'list_introduction': {'quote': 'Items:'}}},
            {'id': 'Q2', 'group_id': 'G2', 'snippet_id': 'S2', 'quote': 'Other rule.',
             'context': {'heading': 'Other', 'list_introduction': None}}]}


def envelope():
    return revision.from_verified_input(source_envelope())[0]


def revised():
    return {'focus': 'Policy',
            'sentences': [{'text': 'The rule applies.', 'source_ids': ['Q1']}]}


def test_group_inputs_are_detached_and_include_only_own_sources():
    source = source_envelope(); original = deepcopy(source)
    groups = revision.from_verified_input(source)
    assert [s['id'] for s in groups[0]['sources']] == ['Q1']
    groups[0]['group']['draft_sentences'].append('Changed.')
    groups[0]['sources'][0]['context']['list_introduction']['quote'] = 'Changed'
    assert source == original
    assert groups[1]['sources'][0]['quote'] == 'Other rule.'



def test_broken_focus_is_replaced_and_unknown_requests_type_distinction():
    inp = envelope()
    inp['group'].update(focus='Clipped...', request='What is the actual value?', status='unknown')
    payload = json.loads(revision.messages(inp)[1]['content'])
    assert payload['focus'] == 'What is the actual value?'
    assert 'Clipped...' not in json.dumps(payload)
    assert 'status' not in payload and 'draft_sentences' not in payload
    assert 'nearby value' in payload['task'] and 'not the requested fact' in payload['task']
    assert 'if these sources establish it' in payload['task']
    inp['group']['status'] = 'supported'
    assert 'nearby value' not in json.loads(revision.messages(inp)[1]['content'])['task']

def test_old_keep_shape_and_duplicate_keys_fail():
    inp = envelope(); original = deepcopy(inp)
    with pytest.raises(ValueError):
        revision.decode('{"focus":"Policy","focus":"Other","sentences":[]}', inp)
    with pytest.raises(ValueError):
        revision.decode(json.dumps({'decision': 'keep', 'reason': '', 'focus': '', 'sentences': []}), inp)
    assert inp == original


def test_inference_messages_exclude_draft_and_status_but_keep_exact_source_context():
    inp = envelope(); inp['group']['status'] = 'unknown-secret-status'
    inp['group']['draft_sentences'] = ['PRIVATE ORIGINAL DRAFT.']
    payload = json.loads(revision.messages(inp)[1]['content'])
    assert 'status' not in payload and 'draft_sentences' not in payload
    assert 'PRIVATE ORIGINAL DRAFT.' not in json.dumps(revision.messages(inp))
    assert payload['sources'] == inp['sources']
    assert payload['request'] == inp['group']['request'] and payload['focus'] == inp['group']['focus']


@pytest.mark.parametrize('change', [
    lambda x: x['sentences'][0].update(source_ids=['Q2']),
    lambda x: x['sentences'][0].update(source_ids=['S1']),
    lambda x: x['sentences'][0].update(source_ids=['Q1', 'Q1']),
    lambda x: x['sentences'][0].update(source_ids=[]),
    lambda x: x.update(focus='x' * 61),
    lambda x: x.update(focus='hidden\x00control'),
    lambda x: x['sentences'][0].update(text='x' * 240 + '.'),
    lambda x: x['sentences'][0].update(text='Unfinished'),
    lambda x: x.update(sentences=[]),
    lambda x: x.update(sentences=[{'text': 'x' * 180 + '.', 'source_ids': ['Q1']}] * 5),
    lambda x: x['sentences'][0].update(extra='forged'),
])
def test_rejects_unbound_citations_and_invalid_output(change):
    value = revised(); change(value)
    with pytest.raises(ValueError): revision.decode(json.dumps(value), envelope())


def test_valid_revision_is_unverified_and_bound_to_input():
    inp = envelope(); result = revision.decode(json.dumps(revised()), inp)
    assert result['kind'] == 'revision_proposal_unverified'
    assert result['input_sha256'] == revision.common.digest(inp)


@pytest.mark.parametrize(('focus', 'code'), [('Incomplete (', 'heading_unbalanced_brackets'),
                                            ('Clipped...', 'heading_ellipsis')])
def test_display_label_retains_generated_focus_and_visible_warning(focus, code):
    inp = envelope(); inp['group']['request'] = 'What is the full policy and its applicable conditions?'
    original = deepcopy(inp); output = revised(); output['focus'] = focus
    result = revision.decode(json.dumps(output), inp)
    assert result['proposal'] == output and inp == original
    assert result['generation_version'] == 'focused-revision-v5'
    assert result['presentation']['version'] == 'request-label-v1'
    assert result['presentation']['display_label'] == inp['group']['request']
    assert result['presentation']['label_source'] == 'original_request'
    assert result['presentation']['generated_focus'] == focus
    assert [w['code'] for w in result['presentation']['heading_warnings']] == [code]


def test_request_label_bound_and_generation_version_are_separate():
    inp = envelope(); inp['group']['request'] = 'R' * 500
    result = revision.decode(json.dumps(revised()), inp)
    assert result['presentation']['display_label'] == 'R' * 500
    assert result['presentation']['heading_warnings'] == []
    assert json.loads(revision.messages(inp)[1]['content'])['version'] == 'focused-revision-v5'
    assert revision.VERSION == 'focused-revision-v6'
    inp['group']['request'] += 'R'
    with pytest.raises(ValueError): revision.decode(json.dumps(revised()), inp)


class Runtime:
    def __init__(self, *, capacity=4096, finish='stop', changed=False, cancel=False):
        self.capacity = capacity; self.finish = finish; self.changed = changed; self.cancel = cancel; self.calls = 0; self.messages = None
    def current_model_id(self): return 'changed' if self.changed and self.calls else 'model'
    def current_model_sha256(self): return 'sha'
    async def validate_investigation_profile(self, *args, **kwargs): pass
    async def measure_investigation_prompt(self, *args, **kwargs):
        return {'input_tokens': 1000, 'context_tokens': self.capacity}
    async def chat(self, *args, **kwargs):
        self.calls += 1
        self.messages = deepcopy(args[0])
        if self.cancel: raise asyncio.CancelledError()
        return {'choices': [{'finish_reason': self.finish, 'message': {'content': json.dumps(revised())}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(('runtime', 'status', 'reason', 'calls'), [
    (Runtime(), 'proposal', None, 1),
    (Runtime(capacity=1699), 'not_generated', 'capacity', 0),
    (Runtime(finish='length'), 'not_generated', 'invalid_output', 1),
    (Runtime(finish=None), 'not_generated', 'invalid_output', 1),
    (Runtime(changed=True), 'not_generated', 'model_changed', 1),
])
async def test_generation_is_bounded_and_does_not_retry(runtime, status, reason, calls):
    result = await revision.generate(envelope(), runtime, model='model', model_sha='sha')
    assert (result['status'], result['reason'], result['generation_calls'], runtime.calls) == (status, reason, calls, calls)
    if status != 'proposal': assert 'result' not in result
    if calls:
        payload = json.loads(runtime.messages[1]['content'])
        assert 'draft_sentences' not in payload and 'status' not in payload
        assert payload['sources'] == result['input']['sources']
        assert result['input']['group']['draft_sentences'] == ['Old answer.']


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry():
    runtime = Runtime(cancel=True)
    with pytest.raises(asyncio.CancelledError):
        await revision.generate(envelope(), runtime, model='model', model_sha='sha')
    assert runtime.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['unchanged', 'stale', 'scope_revoked'])
async def test_preview_rechecks_original_and_scope_without_mutating(monkeypatch, scenario):
    item = {'status': 'draft', 'brief': {'answer': 'original'}, 'evidence': [], 'provenance': {},
            'review': None, 'model_slug': 'model', 'model_sha': 'sha'}
    original = deepcopy(item); calls = []
    async def get(scope, iid):
        calls.append((scope, iid))
        if len(calls) == 2 and scenario == 'scope_revoked': raise LookupError('Unavailable')
        value = deepcopy(item)
        if len(calls) == 2 and scenario == 'stale': value['brief']['answer'] = 'changed'
        return value
    monkeypatch.setattr(investigations, 'get_investigation', get)
    monkeypatch.setattr(investigations, 'get_runtime', lambda: Runtime())
    monkeypatch.setattr(revision, 'prepare_original', lambda item: revision.from_verified_input(source_envelope()))
    if scenario == 'unchanged':
        result = await revision.propose_revision('scope', 'id', 'G1')
        assert result['attempt']['status'] == 'proposal'
        assert result['original_sha256'] == revision.common.digest(original)
    else:
        with pytest.raises(ValueError if scenario == 'stale' else LookupError):
            await revision.propose_revision('scope', 'id', 'G1')
    assert calls == [('scope', 'id'), ('scope', 'id')]
    assert item == original

from test_investigation_verification import setup
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection


def install_revision_reply(runtime, hook=None):
    async def chat(messages, **kwargs):
        runtime.calls.append((messages, kwargs))
        if hook: await hook()
        data = json.loads(messages[1]['content'])
        source_id = data['sources'][0]['id']
        proposal = {'focus': 'Requirement',
                    'sentences': [{'text': 'Managers may deny reimbursement.', 'source_ids': [source_id]}]}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(proposal)}}]}
    runtime.chat = chat



@pytest.mark.asyncio
async def test_prepared_sources_bind_full_same_citation_context_only(setup):
    _, scope, original, _ = await setup()
    prior = deepcopy(original)
    records = [child for e in original['evidence'] for child in (e['sources'] if e['kind'] == 'reviewed_claim' else [e])]
    groups = revision.prepare_original(original)
    for group in groups:
        for source in group['sources']:
            citation = source['context']['citation_id']
            record = max((r for r in records if r['citation_id'] == citation), key=lambda r: len(r.get('presented_content', '')))
            bound = source['context']['cited_chunk']
            assert bound['quote'] == record['presented_content']
            assert bound['quote'][bound['selected_start']:bound['selected_end']] == source['quote']
            assert json.loads(revision.messages(group)[1]['content'])['sources'] == group['sources']
    assert original == prior


def test_prepare_original_does_not_include_unselected_citations(monkeypatch):
    verified = source_envelope()
    verified['source_checks'][0]['context']['citation_id'] = 'C1'
    verified['source_checks'][1]['context']['citation_id'] = 'C2'
    monkeypatch.setattr(investigation_verification, 'original_input', lambda *args, **kwargs: verified)
    item = {'evidence': [
        {'kind': 'document', 'citation_id': 'C1', 'presented_content': 'Prefix. The rule applies. Suffix.'},
        {'kind': 'document', 'citation_id': 'C2', 'presented_content': 'Other rule.'},
        {'kind': 'document', 'citation_id': 'C3', 'presented_content': 'PRIVATE UNSELECTED CITATION.'}],
        'provenance': {'structural_trace': {'offsets': {
            'S1': {'citation_id': 'C1', 'start': 8, 'end': 25},
            'S2': {'citation_id': 'C2', 'start': 0, 'end': 11}}}}}
    group = revision.prepare_original(item)[0]
    assert group['sources'][0]['context']['cited_chunk']['quote'] == 'Prefix. The rule applies. Suffix.'
    assert 'PRIVATE UNSELECTED CITATION.' not in json.dumps(revision.messages(group))
    item['provenance']['structural_trace']['offsets']['S1']['start'] = 0
    with pytest.raises(ValueError, match='binding'): revision.prepare_original(item)

@pytest.mark.asyncio
async def test_real_database_scope_and_preview_preserve_full_original(setup):
    runtime, scope, original, _ = await setup()
    install_revision_reply(runtime)
    with pytest.raises(LookupError):
        await revision.propose_revision(EvidenceScope('other', scope.collection_ids), original['id'], 'G1')
    assert runtime.calls == []
    result = await revision.propose_revision(scope, original['id'], 'G1')
    stored = await investigations.get_investigation(scope, original['id'])
    assert stored == original
    attempt = result['attempt']; assert attempt['status'] == 'proposal'
    allowed = {s['id'] for s in attempt['input']['sources']}
    assert allowed and set(attempt['result']['proposal']['sentences'][0]['source_ids']) <= allowed
    assert all(s['quote'] in json.dumps(original['evidence']) for s in attempt['input']['sources'])
    assert stored['status'] == 'draft' and stored['review'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['rescan', 'review'])
async def test_real_database_change_during_generation_rejects_preview(setup, race):
    runtime, scope, original, path = await setup()
    async def change():
        if race == 'rescan':
            path.write_text('# Expenses\n\nThe original source was replaced.\n')
            await scan_collection('w', scope.collection_ids[0])
        else:
            await investigations.review_investigation(scope, original['id'], 'accept', 'owner', 'Reviewed during generation.')
    install_revision_reply(runtime, change)
    with pytest.raises((LookupError, ValueError)):
        await revision.propose_revision(scope, original['id'], 'G1')
    assert len(runtime.calls) == 1
    if race == 'review':
        current = await investigations.get_investigation(scope, original['id'])
        assert current['status'] == 'reviewed' and current['review'] is not None


@pytest.mark.asyncio
async def test_http_preview_validates_request_and_never_accepts_original(setup):
    import httpx
    from fastapi import FastAPI
    from potluck_local.routers.investigations import router
    runtime, scope, original, _ = await setup()
    install_revision_reply(runtime)
    app = FastAPI(); app.include_router(router)
    payload = {'workspace': scope.workspace, 'collection_ids': list(scope.collection_ids), 'group_id': 'G1', 'seed': 7}
    url = '/company-brain/investigations/' + original['id'] + '/revision-preview'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for invalid in ({'seed': True}, {'seed': -1}, {'group_id': 'G0'}, {'group_id': 'bad'}, {'extra': True}):
            assert (await client.post(url, json={**payload, **invalid})).status_code == 422
        assert (await client.post(url, json={**payload, 'workspace': 'other'})).status_code == 404
        assert (await client.post(url, json={**payload, 'group_id': 'G999'})).status_code == 400
        assert runtime.calls == []
        response = await client.post(url, json=payload)
        assert response.status_code == 200, response.text
        body = response.json(); assert body['attempt']['status'] == 'proposal'
        assert body['attempt']['result']['kind'] == 'revision_proposal_unverified'
        assert body['attempt']['input']['sources'][0]['id'] in body['attempt']['result']['proposal']['sentences'][0]['source_ids']
        assert await investigations.get_investigation(scope, original['id']) == original
        assert len(runtime.calls) == 1


def test_revision_schema_passes_runtime_structured_output_admission():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    response = {'type': 'json_object', 'schema': revision.response_schema(envelope())}
    assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response
    duplicate = revised(); duplicate['sentences'][0]['source_ids'] = ['Q1', 'Q1']
    with pytest.raises(ValueError): revision.decode(json.dumps(duplicate), envelope())


def test_single_group_uses_request_instead_of_old_label_but_multi_group_keeps_focus():
    original = source_envelope()
    original['groups'][0].update(focus='Old short label', request='What is the complete policy?')
    single = deepcopy(original); single['groups'] = single['groups'][:1]
    one = revision.from_verified_input(single)[0]
    assert one['single_focus'] is True
    payload = json.loads(revision.messages(one)[1]['content'])
    assert payload['focus'] == 'What is the complete policy?'
    assert 'Old short label' not in json.dumps(revision.messages(one))
    many = revision.from_verified_input(original)[0]
    assert many['single_focus'] is False
    payload = json.loads(revision.messages(many)[1]['content'])
    assert payload['focus'] == 'Old short label'
    assert payload['request'] == 'What is the complete policy?'
