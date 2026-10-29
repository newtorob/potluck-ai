import json

import pytest

from potluck_local.services import investigation_calculations as calc
from potluck_local.services import investigation_reviewed_operations as ops


def test_classification_preserves_question_text_and_rejects_invalid_plans():
    questions = [{'id': 'Q1', 'text': 'What is current?'}, {'id': 'Q2', 'text': 'What changed?'}]
    assert ops.decode_classification(json.dumps({'Q1': 'answer', 'Q2': 'difference'}), questions) == [
        {'request': 'What is current?', 'operation': 'answer'}, {'request': 'What changed?', 'operation': 'difference'}]
    for rows in ([], [{'request': ' ', 'operation': 'answer'}],
                 [{'request': 'A', 'operation': 'answer'}, {'request': 'a', 'operation': 'difference'}],
                 [{'request': 'A', 'operation': 'run'}]):
        with pytest.raises(ValueError):
            ops.validate_rows(rows)
    with pytest.raises(ValueError):
        ops.decode_classification(json.dumps({'Q1': 'answer'}), questions)


def test_unknowns_preserve_known_inputs_and_conflicts_never_compute(monkeypatch):
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Before 10 ms. After 20 ms.'},
                {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'After 30 ms.'}]
    qs = calc.quantity_catalog(snippets, version='v2')
    plan = ops.plan_with_ids([{'request': 'Change?', 'operation': 'difference'}])
    for status in ('missing', 'unsupported', 'conflicting'):
        data = {'R1': {'status': status, 'operand_ids': ['N1'], 'snippet_ids': ['S2'], 'reason': 'Input uncertain.'}}
        brief, traces = ops.decode_results(json.dumps(data), plan, snippets, qs, [])
        assert traces[0]['result'] is None and traces[0]['inputs'][0]['id'] == 'N1'
        assert set(brief['answers'][0]['snippet_ids']) == {'S1', 'S2'}
        assert traces[0]['limitation_kind'] == status
    monkeypatch.setattr(calc, 'calculate', lambda *a, **k: pytest.fail('conflict must not calculate'))
    data = {'R1': {'status': 'conflicting', 'operand_ids': ['N1', 'N2'], 'snippet_ids': ['S1', 'S2'], 'reason': 'After values conflict.'}}
    assert ops.decode_results(json.dumps(data), plan, snippets, qs, [])[1][0]['status'] == 'conflicting'


def test_zero_denominator_overrides_model_reason_and_incompatible_units():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Before 0 ms. After 2 requests.'}]
    qs = calc.quantity_catalog(snippets, version='v2')
    plan = ops.plan_with_ids([{'request': 'Percent change?', 'operation': 'percent_change'}])
    data = {'R1': {'status': 'missing', 'operand_ids': ['N1', 'N2'], 'snippet_ids': ['S1'], 'reason': 'No baseline.'}}
    brief, traces = ops.decode_results(json.dumps(data), plan, snippets, qs, [])
    assert traces[0]['limitation_kind'] == 'zero_denominator'
    assert 'baseline is zero' in brief['answers'][0]['text'] and 'No baseline' not in brief['answers'][0]['text']


def test_single_zero_operand_does_not_identify_baseline():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'After 0 ms.'}]
    qs = calc.quantity_catalog(snippets, version='v2')
    plan = ops.plan_with_ids([{'request': 'Percent change?', 'operation': 'percent_change'}])
    data = {'R1': {'status': 'missing', 'operand_ids': ['N1'], 'snippet_ids': ['S1'], 'reason': 'The baseline is not recorded.'}}
    brief, traces = ops.decode_results(json.dumps(data), plan, snippets, qs, [])
    assert traces[0]['limitation_kind'] == 'missing'
    assert 'baseline is zero' not in brief['answers'][0]['text']
