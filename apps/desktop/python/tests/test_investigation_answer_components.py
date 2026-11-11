from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_structural_answer as structural

PLAN = [{'id': 'R1', 'operation': 'answer', 'request': 'What does the policy say?'}]
SNIPPETS = [
    {'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Receipts are required for reimbursement.'},
    {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'Receipts are not required for parking under $10.'},
    {'snippet_id': 'S3', 'citation_id': 'C3', 'quote': 'The protocol version is 1.'},
]
TRACE = {'blocks': []}
MODE = {'unknown_mode': 'cited-unknown-v4', 'presentation_mode': 'cited-components-v1'}


@pytest.fixture(autouse=True, params=['cited-components-v1', 'cited-components-v2'])
def component_version(request, monkeypatch):
    monkeypatch.setitem(MODE, 'presentation_mode', request.param)



def component(text, *ids):
    return {'text': text, 'snippet_ids': list(ids)}


def group():
    return {'focus': 'Receipts', 'status': 'supported', 'snippet_ids': ['S1', 'S2'],
            'conditions': [], 'exceptions': [component('Parking under $10 is exempt.', 'S2')],
            'distinctions': [], 'conclusion': 'Receipts are required for reimbursement.',
            'unknown_reason': ''}


def decode(g, snippets=SNIPPETS, mode=MODE):
    return structural.decode_results(json.dumps({'R1': {'groups': [g], 'more': False}}),
        PLAN, snippets, [], [], structural_trace=TRACE, **mode)


def test_exception_is_visible_with_its_binding_and_quotes_remain_exact():
    brief, _, coverage = decode(group())
    text = brief['answers'][0]['text']
    assert 'Receipts are required for reimbursement.' in text
    assert 'Parking under $10 is exempt.' in text and 'S2' in text
    assert brief['answers'][0]['source_excerpts'] == SNIPPETS[:2]
    assert [c['text'] for c in brief['answers'][0]['parts'][0]['claims']] == [s['quote'] for s in SNIPPETS[:2]]
    entry = coverage['cited_conclusions']['operations']['R1'][0]
    assert entry['components']['exceptions'] == group()['exceptions']
    assert entry['primary_conclusion'] == group()['conclusion']
    assert entry['primary_unknown_reason'] == ''
    assert 'Parking under $10 is exempt.' in entry['text']
    structural.validate_original(brief, coverage, SNIPPETS, TRACE, **MODE)


def test_unknown_distinguishes_protocol_from_release():
    g = dict(group(), focus='First release', status='unknown', snippet_ids=['S3'],
             exceptions=[], conclusion='', unknown_reason='The first release is not identified.',
             distinctions=[component('Protocol version 1 is not a release version.', 'S3')])
    brief, _, coverage = decode(g)
    assert 'The first release is not identified.' in brief['answers'][0]['text']
    assert 'Protocol version 1 is not a release version.' in brief['answers'][0]['text']
    assert 'S3' in brief['answers'][0]['text']
    structural.validate_original(brief, coverage, SNIPPETS, TRACE, **MODE)


@pytest.mark.parametrize('mutation', [
    lambda g: g['exceptions'][0].update(snippet_ids=['S3']),
    lambda g: g['exceptions'][0].update(snippet_ids=['S99']),
    lambda g: g['exceptions'][0].update(snippet_ids=[]),
    lambda g: g['exceptions'][0].update(snippet_ids=['S1', 'S2', 'S3']),
    lambda g: g.update(distinctions=[component('A distinction.', 'S1')]),
    lambda g: g.update(status='unknown', conclusion='', unknown_reason='Unknown.'),
    lambda g: g.update(status='conflicting', conclusion=''),
    lambda g: g.update(conclusion=''),
    lambda g: g.update(unknown_reason='Unexpected.'),
    lambda g: g.update(exceptions=None),
    lambda g: g.update(exceptions={}),
    lambda g: g.pop('conditions'),
    lambda g: g.update(exceptions=[component('An exception.', 'S1')] * 3),
    lambda g: g['exceptions'][0].update(text='x' * 161),
    lambda g: g['exceptions'][0].update(text='A forged claim [S1].'),
    lambda g: g.update(conclusion='A forged claim [S2].'),
])
def test_invalid_components_are_rejected(mutation):
    g = group()
    mutation(g)
    with pytest.raises(ValueError):
        decode(g)


def test_composed_text_overflow_is_rejected_without_trimming():
    g = group()
    g['conclusion'] = 'A' * 150 + '.'
    g['exceptions'] = [component('B' * 150 + '.', 'S2')]
    if MODE['presentation_mode'] == 'cited-components-v1':
        with pytest.raises(ValueError): decode(g)
    else:
        brief, _, _ = decode(g)
        assert g['conclusion'] in brief['answers'][0]['text']
        assert g['exceptions'][0]['text'] in brief['answers'][0]['text']


@pytest.mark.parametrize('change', ['component_text', 'component_source', 'primary', 'source_quote', 'display'])
def test_original_replay_rejects_component_or_source_tampering(change):
    brief, _, coverage = decode(group())
    snippets = deepcopy(SNIPPETS)
    entry = coverage['cited_conclusions']['operations']['R1'][0]
    if change == 'component_text': entry['components']['exceptions'][0]['text'] = 'No exceptions.'
    if change == 'component_source': entry['components']['exceptions'][0]['snippet_ids'] = ['S1']
    if change == 'primary': entry['primary_conclusion'] = 'No receipt is required.'
    if change == 'source_quote': snippets[1]['quote'] = 'Receipts are always required.'
    if change == 'display': brief['answers'][0]['text'] = brief['answers'][0]['text'].replace('S2', 'S1')
    with pytest.raises(ValueError):
        structural.validate_original(brief, coverage, snippets, TRACE, **MODE)


def test_legacy_presentation_still_decodes_and_replays():
    legacy = dict(MODE, presentation_mode='cited-conclusions-v6')
    g = {k: v for k, v in group().items() if k not in ('conditions', 'exceptions', 'distinctions')}
    brief, _, coverage = decode(g, mode=legacy)
    assert g['conclusion'] in brief['answers'][0]['text']
    structural.validate_original(brief, coverage, SNIPPETS, TRACE, **legacy)


def test_schema_is_source_bound_and_captures_qualifications_before_primary_text():
    from potluck_local.services import investigation_answer_presentation as presentation
    from potluck_local.services import investigation_aspect_source_answer_v20 as base
    from potluck_local.services import investigation_unknown_explanations as unknowns
    schema = unknowns.response_schema(base.response_schema(PLAN, SNIPPETS, []), PLAN)
    original = deepcopy(schema)
    result = presentation.response_schema(schema, PLAN, version=MODE['presentation_mode'])
    assert schema == original
    props = result['properties']['R1']['properties']['groups']['items']['properties']
    assert list(props) == ['focus', 'status', 'snippet_ids', 'conditions', 'exceptions', 'distinctions', 'conclusion', 'unknown_reason']
    assert props['exceptions']['items']['properties']['snippet_ids']['items']['enum'] == ['S1', 'S2', 'S3']
    assert props['focus']['maxLength'] == 60


@pytest.mark.parametrize('mutation', [
    lambda g: g.update(focus='x' * 61),
    lambda g: g['exceptions'][0].update(snippet_ids=['S2', 'S2']),
    lambda g: g['exceptions'][0].update(text='A forged marker [s2].'),
    lambda g: g['exceptions'][0].update(text='A hidden\u200bcontrol.'),
    lambda g: g['exceptions'][0].update(extra='ignored'),
    lambda g: g.update(exceptions=[g['exceptions'][0]] * 2),
    lambda g: g['exceptions'][0].update(text=' ' * 161 + 'A.'),
])
def test_additional_bounds_and_unknown_fields_are_rejected(mutation):
    g = group(); mutation(g)
    with pytest.raises(ValueError): decode(g)


def test_ordinary_f1_text_is_not_mistaken_for_a_citation_marker():
    g = group(); g['exceptions'][0]['text'] = 'F1 scores are excluded.'
    brief, _, _ = decode(g)
    assert 'F1 scores are excluded.' in brief['answers'][0]['text']


def test_missing_fact_and_component_primary_text_are_tamper_checked():
    g = dict(group(), status='unknown', snippet_ids=['S3'], exceptions=[], conclusion='',
             unknown_reason='The first release is not identified.',
             distinctions=[component('Protocol version 1 is not a release version.', 'S3')])
    brief, _, coverage = decode(g)
    original = deepcopy(coverage)
    coverage['cited_conclusions']['operations']['R1'][0]['primary_unknown_reason'] = 'No release exists.'
    with pytest.raises(ValueError): structural.validate_original(brief, coverage, SNIPPETS, TRACE, **MODE)
    assert original != coverage
