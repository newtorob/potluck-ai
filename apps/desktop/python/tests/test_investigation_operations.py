import json

import pytest

from potluck_local.services import investigation_calculations as calc
from potluck_local.services import investigation_operations as ops


def test_plan_requires_every_question_and_assigns_ids():
    questions = [{'id': 'Q1', 'text': 'First'}, {'id': 'Q2', 'text': 'Second'}]
    entries = [{'question_id': 'Q1', 'request': 'How much?', 'operation': 'difference'},
               {'question_id': 'Q2', 'request': 'Why?', 'operation': 'answer'}]
    plan = ops.validate_plan(entries, questions)
    assert [x['id'] for x in plan] == ['R1', 'R2']
    assert ops.decode_plan(json.dumps({'operations': entries}), questions) == plan
    for invalid in (entries[:1], entries + [entries[0] | {'id': 'R9'}],
                    entries + [entries[0] | {'operation': 'execute'}], entries * 7):
        with pytest.raises(ValueError):
            ops.validate_plan(invalid, questions)


def test_every_operation_is_required_and_calculations_are_server_generated():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Before 100 ms. After 200 ms.'},
                {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'Before 10%. After 20%.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': op, 'operation': op}
                              for op in ops.OPERATIONS], [{'id': 'Q1', 'text': 'all'}])
    data = {'R1': {'status': 'unknown', 'text': 'Not established.', 'snippet_ids': []},
            'R2': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Source quantities selected.'},
            'R3': {'status': 'ready', 'operand_ids': ['N3', 'N4'], 'snippet_ids': [], 'reason': 'Source quantities selected.'},
            'R4': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Source quantities selected.'},
            'R5': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Source quantities selected.'}}
    brief, traces = ops.decode_results(json.dumps(data), plan, snippets, quantities)
    assert [t['result'] for t in traces] == ['0.1', '10', '100', '50']
    assert all(a['source_excerpts'] for a in brief['answers'][1:])
    schema = ops.response_schema(plan, snippets, quantities)
    assert schema['required'] == ['R1', 'R2', 'R3', 'R4', 'R5']
    for mutation in ({'R1': data['R1']}, data | {'R2': data['R2'] | {'result': '999'}},
                     data | {'R2': data['R2'] | {'operand_ids': ['N1', 'N99']}},
                     data | {'R2': data['R2'] | {'operand_ids': ['N1', 'N1']}}):
        with pytest.raises(ValueError):
            ops.decode_results(json.dumps(mutation), plan, snippets, quantities)


def test_conflict_is_never_calculated_and_correction_cannot_forge_trace():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '100 ms and 200 ms.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Difference', 'operation': 'difference'}],
                             [{'id': 'Q1', 'text': 'Difference'}])
    raw = {'R1': {'status': 'conflicting', 'operand_ids': ['N1', 'N2'], 'snippet_ids': ['S1'], 'reason': 'Authority differs.'}}
    brief, traces = ops.decode_results(json.dumps(raw), plan, snippets, quantities)
    assert traces[0]['result'] is None and brief['answers'][0]['status'] == 'conflicting'
    changed = json.loads(json.dumps(brief))
    changed['answers'][0]['calculation']['result'] = '999'
    with pytest.raises(ValueError):
        ops.corrected_brief(changed, brief, snippets)
    changed = json.loads(json.dumps(brief))
    changed['answers'][0]['text'] = 'Reviewer interpretation.'
    assert 'calculation' not in ops.corrected_brief(changed, brief, snippets)['answers'][0]


def test_reject_duplicate_operations_after_normalization():
    questions = [{'id': 'Q1', 'text': 'Question'}]
    plan = [{'question_id': 'Q1', 'request': ' Difference ', 'operation': 'difference'},
            {'question_id': 'Q1', 'request': 'Difference', 'operation': 'difference'}]
    with pytest.raises(ValueError, match='Duplicate'):
        ops.validate_plan(plan, questions)


@pytest.mark.parametrize('status', ['unknown', 'conflicting'])
def test_uncertain_calculations_do_not_compute_and_include_inputs(status):
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Before 100 ms.'},
                {'snippet_id': 'S2', 'citation_id': 'C2', 'quote': 'After 200 ms.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Change?', 'operation': 'difference'}],
                             [{'id': 'Q1', 'text': 'Change?'}])
    data = {'R1': {'status': status, 'operand_ids': ['N1', 'N2'],
                   'snippet_ids': ['S1'], 'reason': 'Evidence is incomplete or disputed.'}}
    brief, traces = ops.decode_results(json.dumps(data), plan, snippets, quantities)
    assert traces[0]['result'] is None and traces[0]['status'] == status
    assert brief['answers'][0]['snippet_ids'] == ['S1', 'S2']
    assert 'Calculation unavailable' in brief['answers'][0]['text']


def test_missing_and_zero_denominator_are_unknown():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Before 0 ms. After 200 ms.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Change?', 'operation': 'percent_change'}],
                             [{'id': 'Q1', 'text': 'Change?'}])
    for ids in (['N1'], ['N1', 'N2']):
        data = {'R1': {'status': 'ready', 'operand_ids': ids, 'snippet_ids': [], 'reason': 'Source quantities selected.'}}
        brief, traces = ops.decode_results(json.dumps(data), plan, snippets, quantities)
        assert traces[0]['status'] == 'unknown' and traces[0]['result'] is None
        assert brief['answers'][0]['status'] == 'unknown'


@pytest.mark.parametrize('bad_id', [{}, [], None, 1])
def test_trace_correction_rejects_nonstring_question_id(bad_id):
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '1 ms and 2 ms.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Change?', 'operation': 'difference'}],
                             [{'id': 'Q1', 'text': 'Change?'}])
    raw = {'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'], 'snippet_ids': [], 'reason': 'Source quantities selected.'}}
    brief, _ = ops.decode_results(json.dumps(raw), plan, snippets, quantities)
    correction = json.loads(json.dumps(brief))
    correction['answers'][0]['question_id'] = bad_id
    with pytest.raises(ValueError):
        ops.corrected_brief(correction, brief, snippets)


def test_maximum_mixed_schema_compiles_compact_grammar_in_subprocess():
    import os
    import subprocess
    import sys
    script = '''
import json
from llama_cpp import LlamaGrammar
from potluck_local.services import investigation_operations as ops
questions = [{'id': 'Q1', 'text': 'Issue'}]
plan = [{'id': f'R{i}', 'question_id': 'Q1', 'request': 'Request',
         'operation': 'answer' if i % 2 else 'difference'} for i in range(1, 13)]
snippets = [{'snippet_id': f'S{i}', 'quote': 'Source'} for i in range(1, 51)]
quantities = [{'id': f'N{i}'} for i in range(1, 101)]
for schema in (ops.planner_schema(questions), ops.response_schema(plan, snippets, quantities)):
    grammar = LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)._grammar
    assert len(grammar) < 40000
    assert '(char (char (char (char' not in grammar
'''
    env = os.environ.copy()
    env.pop('LLAMA_CPP_LIB_PATH', None)
    result = subprocess.run([sys.executable, '-c', script], text=True, capture_output=True,
                            timeout=20, env=env)
    assert result.returncode == 0, result.stderr


def test_ready_reason_still_enforces_server_boundaries():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': '1 ms and 2 ms.'}]
    quantities = calc.quantity_catalog(snippets)
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Change?', 'operation': 'difference'}],
                             [{'id': 'Q1', 'text': 'Change?'}])
    for reason in ('', ' ', 'x' * 1001):
        data = {'R1': {'status': 'ready', 'operand_ids': ['N1', 'N2'],
                       'snippet_ids': [], 'reason': reason}}
        with pytest.raises(ValueError):
            ops.decode_results(json.dumps(data), plan, snippets, quantities)


def test_empty_quantity_catalog_requires_no_operands_and_returns_unknown():
    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Seven jobs completed.'}]
    plan = ops.validate_plan([{'question_id': 'Q1', 'request': 'Percentage?', 'operation': 'percentage'}],
                             [{'id': 'Q1', 'text': 'Percentage?'}])
    quantities = calc.quantity_catalog(snippets)
    assert quantities == []
    schema = ops.response_schema(plan, snippets, quantities)
    assert schema['properties']['R1']['properties']['operand_ids']['maxItems'] == 0
    from llama_cpp import LlamaGrammar
    grammar = LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)._grammar
    assert 'R1-operand-ids ::= "[" space  "]" space' in grammar
    result = {'R1': {'status': 'ready', 'operand_ids': [], 'snippet_ids': [],
                     'reason': 'No numerical quantities available.'}}
    brief, trace = ops.decode_results(json.dumps(result), plan, snippets, quantities)
    assert trace[0]['status'] == 'unknown' and brief['answers'][0]['status'] == 'unknown'
    result['R1']['operand_ids'] = ['S1']
    with pytest.raises(ValueError):
        ops.decode_results(json.dumps(result), plan, snippets, quantities)
