from copy import deepcopy
import json

import pytest

from potluck_local.services import investigation_draft_verification as v
from potluck_local.services import investigation_structural_answer as structural


def draft():
    plan = [{'id': 'R1', 'operation': 'answer', 'request': 'What is the policy?'}]
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Receipts are required. Managers may make exceptions.'},
                {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'Parking is exempt below $10.'}]
    group = {'focus': 'Receipts', 'status': 'supported', 'snippet_ids': ['S1', 'S2'],
             'conditions': [{'text': 'The policy applies to expenses.', 'snippet_ids': ['S1']}],
             'exceptions': [{'text': 'Parking below $10 is exempt.', 'snippet_ids': ['S2']}],
             'distinctions': [], 'conclusion': 'Receipts are required.', 'unknown_reason': ''}
    _, _, coverage = structural.decode_results(json.dumps({'R1': {'groups': [group], 'more': False}}),
        plan, snippets, [], [], structural_trace={'blocks': []}, unknown_mode='cited-unknown-v4',
        presentation_mode='cited-components-v1')
    return {'issue': 'Explain the policy.', 'provenance': {'operation_plan': plan,
            'snippet_catalog': snippets, 'answer_coverage': coverage}}


def clear(envelope):
    return {'source_checks': {x['id']: {'verdict': 'adequate', 'reason': 'Complete.', 'omitted_quote': ''} for x in envelope['source_checks']},
            'claim_checks': {x['id']: {'verdict': 'supported', 'reason': 'Supported.'} for x in envelope['claims']},
            'group_checks': {x['id']: {'verdict': 'clear', 'reason': 'Complete.'} for x in envelope['groups']}}


def test_bindings_components_and_original_unchanged():
    item = draft(); original = deepcopy(item); envelope = v.prepare(item)
    assert item == original
    assert [x['quote'] for x in envelope['source_checks']] == [x['quote'] for x in item['provenance']['snippet_catalog']]
    assert [(x['kind'], x['allowed_snippet_ids']) for x in envelope['claims']] == [
        ('primary', ['S1', 'S2']), ('conditions', ['S1']), ('exceptions', ['S2'])]
    assert envelope['scope'] == 'original cited passages only; unselected evidence not checked'
    result = v.decode(json.dumps(clear(envelope)), envelope)
    assert result['status'] == 'no_issue_found' and result['input_sha256'] == v.digest(envelope)
    assert 'verified' not in result['status']


@pytest.mark.parametrize('change', ['missing', 'forged', 'altered'])
def test_original_source_must_match_catalog(change):
    item = draft(); entry = item['provenance']['answer_coverage']['cited_conclusions']['operations']['R1'][0]
    if change == 'missing': entry['source_excerpts'].pop()
    if change == 'forged': entry['components']['exceptions'][0]['snippet_ids'] = ['S99']
    if change == 'altered': entry['source_excerpts'][0]['quote'] = 'Changed.'
    with pytest.raises(ValueError): v.prepare(item)


def test_omission_requires_exact_quote_and_forces_review():
    envelope = v.prepare(draft()); response = clear(envelope)
    response['source_checks']['Q1'] = {'verdict': 'omission', 'reason': 'Discretion is missing.', 'omitted_quote': 'Managers may make exceptions.'}
    assert v.decode(json.dumps(response), envelope)['status'] == 'needs_review'
    response['source_checks']['Q1']['omitted_quote'] = 'Managers must make exceptions.'
    with pytest.raises(ValueError): v.decode(json.dumps(response), envelope)
    response['source_checks']['Q1']['verdict'] = 'adequate'
    with pytest.raises(ValueError): v.decode(json.dumps(response), envelope)


@pytest.mark.parametrize('mutation', [
    lambda x: x['claim_checks']['C1'].update(verdict='verified'),
    lambda x: x['group_checks']['G1'].update(extra=True),
    lambda x: x['source_checks'].pop('Q1'),
    lambda x: x['claim_checks']['C1'].update(reason='bad\ntext'),
    lambda x: x.update(extra={}),
])
def test_invalid_response_rejected(mutation):
    envelope = v.prepare(draft()); response = clear(envelope); mutation(response)
    with pytest.raises(ValueError): v.decode(json.dumps(response), envelope)


def test_duplicate_json_keys_rejected():
    envelope = v.prepare(draft())
    with pytest.raises(ValueError): v.decode('{"source_checks":{},"source_checks":{},"claim_checks":{},"group_checks":{}}', envelope)


def test_empty_numeric_only_inventory():
    item = draft(); p = item['provenance']; p['operation_plan'][0]['operation'] = 'sum'
    p['answer_coverage']['cited_conclusions']['operations'] = {}
    p['answer_coverage']['unknown_explanations']['operations'] = {}
    envelope = v.prepare(item)
    assert envelope['groups'] == envelope['source_checks'] == envelope['claims'] == []
    assert v.decode(json.dumps(clear(envelope)), envelope)['status'] == 'no_issue_found'
    assert list(v.response_schema(envelope)['properties']) == ['source_checks', 'claim_checks', 'group_checks']


def test_multiline_omission_preserves_exact_source_bytes():
    envelope = v.prepare(draft()); envelope['source_checks'][0]['quote'] = 'Receipts required.\nManagers may\ndeny claims.'
    response = clear(envelope)
    response['source_checks']['Q1'].update(verdict='omission', reason='Discretion omitted.', omitted_quote='Managers may\ndeny claims.')
    assert v.decode(json.dumps(response), envelope)['status'] == 'needs_review'
    response['source_checks']['Q1']['omitted_quote'] = 'Managers may deny claims.'
    with pytest.raises(ValueError): v.decode(json.dumps(response), envelope)


def test_no_cited_source_cannot_be_cleared_by_model():
    envelope = v.prepare(draft()); envelope['claims'][0]['allowed_snippet_ids'] = []
    assert v.decode(json.dumps(clear(envelope)), envelope)['status'] == 'needs_review'
