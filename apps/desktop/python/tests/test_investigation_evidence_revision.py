from copy import deepcopy
import json
import pytest

from potluck_local.services import investigation_evidence_revision as ev
from potluck_local.services import investigation_revision as revision


def sample():
    text = 'Policy applies. Prior approval is required.\n\nA director may defer the request.'
    quote = 'Prior approval is required.'; start = text.index(quote)
    return {'version': revision.VERSION, 'scope': 'cited', 'group': {'id': 'G1', 'focus': 'Policy', 'request': 'What applies?',
        'status': 'unknown', 'draft_sentences': ['PRIVATE DRAFT.']}, 'sources': [
        {'id': 'Q1', 'snippet_id': 'S1', 'quote': quote, 'context': {'citation_id': 'C1', 'heading': 'Policy',
            'list_introduction': None, 'cited_chunk': {'quote': text, 'selected_start': start, 'selected_end': start + len(quote)}}}]}


def decisions(prepared, verdict='source_rule'):
    return {'verdict': verdict, 'units': {u['id']: 'background' for u in prepared['units']}}


def test_exact_units_aliases_and_separate_citations():
    inp = sample(); duplicate = deepcopy(inp['sources'][0]); duplicate['id'] = 'Q2'; duplicate['snippet_id'] = 'S2'
    other = deepcopy(duplicate); other['id'] = 'Q3'; other['context']['citation_id'] = 'C2'
    inp['sources'] += [duplicate, other]; before = deepcopy(inp); result = ev.prepare(inp)
    assert inp == before and len(result['contexts']) == 2
    for context in result['contexts']:
        units = [u for u in result['units'] if u['citation_id'] == context['citation_id']]
        assert ''.join(u['quote'] for u in units) == context['text']
        assert all(context['text'][u['start']:u['end']] == u['quote'] for u in units)
    assert result['contexts'][0]['source_ids'] == ['Q1', 'Q2']
    assert result['contexts'][0]['source_contexts'] == [
        {'source_id': sid, 'heading': 'Policy', 'list_introduction': None}
        for sid in ('Q1', 'Q2')]
    assert result['contexts'][1]['source_ids'] == ['Q3']
    inp['sources'][0]['quote'] = 'altered'
    assert result['original'] == before


def test_uncited_fixtures_do_not_merge_and_keep_heading_context():
    inp = sample(); source = inp['sources'][0]; source['context'] = {'heading': 'Heading', 'list_introduction': {'quote': 'Intro:'}}
    another = deepcopy(source); another['id'] = 'Q2'; inp['sources'].append(another)
    result = ev.prepare(inp)
    assert len(result['contexts']) == 2
    assert result['units'][0]['source_contexts'][0]['list_introduction']['quote'] == 'Intro:'


@pytest.mark.parametrize('change', [
    lambda e: e['sources'][0]['context']['cited_chunk'].update(selected_start=0),
    lambda e: e['sources'][0]['context']['cited_chunk'].update(selected_end=True),
    lambda e: e['sources'].append(deepcopy(e['sources'][0])),
    lambda e: e['sources'][0]['context'].update(list_introduction={'start': 0, 'end': 3, 'quote': 'forged'}),
])
def test_rejects_invalid_bindings(change):
    inp = sample(); change(inp)
    with pytest.raises(ValueError): ev.prepare(inp)


def test_bounds_are_explicit_and_no_clipping():
    inp = sample(); inp['sources'][0]['quote'] = 'x' * 12001
    inp['sources'][0]['context'].pop('cited_chunk')
    with pytest.raises(ValueError, match='character limit'): ev.prepare(inp)
    inp['sources'][0]['quote'] = 'Sentence ends. ' * 49 + 'End.'
    with pytest.raises(ValueError, match='unit limit'): ev.prepare(inp)


def test_model_input_excludes_original_draft_and_status():
    inp = ev.prepare(sample()); payload = json.loads(ev.messages(inp)[1]['content'])
    assert 'PRIVATE DRAFT' not in json.dumps(ev.messages(inp))
    assert 'status' not in payload and payload['units'] == inp['units']


def test_selected_and_uncertain_are_exact_and_always_unverified():
    inp = ev.prepare(sample()); data = decisions(inp)
    data['units'][inp['units'][0]['id']] = 'condition'
    data['units'][inp['units'][1]['id']] = 'exception'
    data['units'][inp['units'][-1]['id']] = 'uncertain'
    out = ev.decode(json.dumps(data), inp)
    assert out['needs_review'] is True and out['kind'] == 'evidence_selection_unverified'
    assert [u['quote'] for u in out['selected']] == [inp['units'][0]['quote'], inp['units'][1]['quote']]
    assert out['uncertain'][0]['quote'] == inp['units'][-1]['quote']
    assert out['input_sha256'] == ev.common.digest(inp)


@pytest.mark.parametrize('alter', ['missing', 'extra', 'bad', 'no_evidence', 'duplicate'])
def test_decision_inventory_and_verdict_rejected(alter):
    inp = ev.prepare(sample()); data = decisions(inp)
    if alter == 'missing': data['units'].pop(next(iter(data['units'])))
    if alter == 'extra': data['units']['U999'] = 'answer'
    if alter == 'bad': data['units']['U1'] = 'approved'
    if alter == 'duplicate':
        raw = '{"verdict":"yes","units":{"U1":"answer","U1":"condition"}}'
    else: raw = json.dumps(data)
    with pytest.raises(ValueError): ev.decode(raw, inp)


def test_not_stated_keeps_distinction_and_context():
    inp = ev.prepare(sample()); data = decisions(inp, 'not_stated'); data['units']['U1'] = 'distinction'
    out = ev.decode(json.dumps(data), inp)
    assert out['selected'][0]['classification'] == 'distinction'
    assert out['selected'][0]['quote'] == inp['units'][0]['quote'] and out['needs_review']


def test_schema_passes_native_admission():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    response = {'type': 'json_object', 'schema': ev.response_schema(ev.prepare(sample()))}
    assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response


def test_multi_group_input_focus_and_full_question_display():
    original = sample(); original['single_focus'] = False
    original['group']['focus'] = 'Specific topic'
    inp = ev.prepare(original)
    assert json.loads(ev.messages(inp)[1]['content'])['request'] == 'Specific topic'
    data = decisions(inp, 'not_stated')
    assert ev.decode(json.dumps(data), inp)['question'] == 'What applies?'
    original['single_focus'] = True
    assert json.loads(ev.messages(ev.prepare(original))[1]['content'])['request'] == 'What applies?'


@pytest.mark.asyncio
async def test_bounded_service_uses_contract_without_mutating_input():
    from potluck_local.services import investigation_evidence_revision_service as service
    inp = ev.prepare(sample()); before = deepcopy(inp)
    class Runtime:
        calls = 0
        def current_model_id(self): return 'model'
        def current_model_sha256(self): return 'sha'
        async def validate_investigation_profile(self, *args, **kwargs): pass
        async def measure_investigation_prompt(self, *args, **kwargs):
            return {'input_tokens': 100, 'context_tokens': 8192}
        async def chat(self, *args, **kwargs):
            self.calls += 1
            data = decisions(inp, 'conditional'); data['units']['U1'] = 'condition'
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}]}
    runtime = Runtime(); result = await service.generate(inp, runtime, model='model', model_sha='sha')
    assert result['status'] == 'proposal' and result['result']['needs_review'] is True
    assert result['result']['verdict'] == 'conditional' and runtime.calls == 1 and inp == before
