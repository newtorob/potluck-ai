import json
from copy import deepcopy

import pytest

from potluck_local.services import investigation_bounded_answers as v13

PLAN = [{'id': 'R1', 'question_id': 'Q1', 'request': 'What is ready and what is blocked?', 'operation': 'answer'}]
SNIPPETS = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Pilot is ready.'}]


def test_planner_accepts_labels_and_rejects_bad_shapes():
    parts, gaps = v13.decode_plan(json.dumps({'R1': ['ready', 'blocked']}), PLAN)
    assert parts['R1'] == [{'id': 'R1P1', 'request_span': 'ready'}, {'id': 'R1P2', 'request_span': 'blocked'}]
    assert gaps['R1']['mode'] == 'request_spans'
    for value in ([], ['a'] * 5, [''], [1]):
        with pytest.raises(ValueError):
            v13.decode_plan(json.dumps({'R1': value}), PLAN)
    for raw in ('{"R1":["a"],"R1":["b"]}', '{"R2":["a"]}', '{"R1":["bad\nlabel"]}'):
        with pytest.raises(ValueError):
            v13.decode_plan(raw, PLAN)


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_schema_and_validator_share_exact_boundaries(count):
    parts, _ = v13.decode_plan(json.dumps({'R1': ['What', 'is', 'ready', 'blocked'][:count]}), PLAN)
    schema = v13.response_schema(PLAN, SNIPPETS, [], parts)
    budget = min(1000, (4000 - (count - 1)) // count)
    values = {p['id']: {'status': 'supported', 'text': 'x' * budget, 'snippet_ids': ['S1']} for p in parts['R1']}
    assert all(schema['properties']['R1']['properties'][p['id']]['properties']['text']['maxLength'] == budget for p in parts['R1'])
    brief, _, coverage = v13.decode_results(json.dumps({'R1': values}), PLAN, SNIPPETS, [], [], parts)
    assert len(brief['answers'][0]['text']) <= 4000
    if count >= 2:
        assert len(brief['answers'][0]['text']) > 1000
    assert brief['questions'][0]['text'] == PLAN[0]['request']
    assert 'not verified' in coverage['coverage_assessment']
    values[parts['R1'][0]['id']]['text'] += 'x'
    with pytest.raises(ValueError):
        v13.decode_results(json.dumps({'R1': values}), PLAN, SNIPPETS, [], [], parts)


def test_citations_status_shape_and_corrections():
    parts, _ = v13.decode_plan('{"R1":["ready","blocked"]}', PLAN)
    supported = {'status': 'supported', 'text': 'Ready.', 'snippet_ids': ['S1']}
    unknown = {'status': 'unknown', 'text': 'Unknown.', 'snippet_ids': []}
    values = {'R1P1': supported, 'R1P2': unknown}
    brief, _, _ = v13.decode_results(json.dumps({'R1': values}), PLAN, SNIPPETS, [], [], parts)
    assert brief['answers'][0]['status'] == 'unknown'
    correction = v13.corrected_brief(deepcopy(brief), brief, SNIPPETS)
    assert 'parts' not in correction['answers'][0]
    changed = deepcopy(brief)
    changed['answers'][0]['parts'][0]['request_span'] = 'Other'
    with pytest.raises(ValueError):
        v13.corrected_brief(changed, brief, SNIPPETS)
    for invalid in ({'R1P1': supported}, {**values, 'foreign': unknown},
                    {**values, 'R1P1': {**supported, 'snippet_ids': ['foreign']}},
                    {**values, 'R1P1': {**supported, 'snippet_ids': ['S1', 'S1']}},
                    {**values, 'R1P1': {**supported, 'snippet_ids': []}},
                    {**values, 'R1P1': {**supported, 'status': 'invalid'}}):
        with pytest.raises(ValueError):
            v13.decode_results(json.dumps({'R1': invalid}), PLAN, SNIPPETS, [], [], parts)
    value = json.dumps(supported)
    with pytest.raises(ValueError):
        v13.decode_results('{"R1":{"R1P1":' + value + ',"R1P1":' + value + '}}', PLAN, SNIPPETS, [], [], parts)
    conflict = {**values, 'R1P2': {'status': 'conflicting', 'text': 'Disagreement.', 'snippet_ids': ['S1']}}
    assert v13.decode_results(json.dumps({'R1': conflict}), PLAN, SNIPPETS, [], [], parts)[0]['answers'][0]['status'] == 'conflicting'


def test_label_and_raw_json_limits():
    assert v13.decode_plan(json.dumps({'R1': ['x' * 120]}), PLAN)[1]['R1']['mode'] == 'original_request_fallback'
    with pytest.raises(ValueError):
        v13.decode_plan(json.dumps({'R1': ['x' * 501]}), PLAN)
    parts, _ = v13.decode_plan('{"R1":["Ready"]}', PLAN)
    with pytest.raises(ValueError):
        v13.decode_results('{"R1":{"R1P1":{"status":"unknown","text":"bad\ntext","snippet_ids":[]}}}', PLAN, SNIPPETS, [], [], parts)
    assert v13.part_text_limits(parts) == {'R1P1': 1000}


def test_mixed_and_calculation_only_decode():
    from potluck_local.services import investigation_calculations as calc
    from potluck_local.services import investigation_reviewed_operations as v9

    snippets = [{'snippet_id': 'S1', 'citation_id': 'C1', 'quote': 'Alpha 10 ms. Beta 20 ms.'}]
    quantities = calc.quantity_catalog(snippets, version='v3')
    calculation = {'status': 'ready', 'minuend_id': 'N1', 'subtrahend_id': 'N2',
                   'known_input_snippet_ids': ['S1'], 'competing_snippet_ids': [],
                   'limitation_snippet_ids': [], 'limitation_basis': 'none', 'reason': 'Selected values.'}
    calc_plan = v9.plan_with_ids([{'request': 'Alpha minus beta?', 'operation': 'difference'}])
    brief, traces, _ = v13.decode_results(json.dumps({'R1': calculation}), calc_plan, snippets, quantities, [], {}, version='v3')
    assert traces[0]['result'] == '-0.01'
    assert brief['answers'][0]['calculation'] == traces[0]
    mixed = v9.plan_with_ids([{'request': 'What is alpha?', 'operation': 'answer'},
                              {'request': 'Alpha minus beta?', 'operation': 'difference'}])
    parts, _ = v13.decode_plan('{"R1":["alpha"]}', mixed)
    raw = {'R1': {'R1P1': {'status': 'supported', 'text': '10 ms.', 'snippet_ids': ['S1']}}, 'R2': calculation}
    brief, traces, _ = v13.decode_results(json.dumps(raw), mixed, snippets, quantities, [], parts, version='v3')
    assert brief['answers'][0]['parts'][0]['request_span'] == 'alpha'
    assert traces[0]['result'] == '-0.01'
    assert [a['question_id'] for a in brief['answers']] == ['R1', 'R2']
    forged = deepcopy(brief)
    forged['answers'][1]['calculation']['result'] = '999'
    with pytest.raises(ValueError):
        v13.corrected_brief(forged, brief, snippets)


def test_runtime_grammar_conversion():
    llama_cpp = pytest.importorskip('llama_cpp')
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    parts, _ = v13.decode_plan('{"R1":["ready","blocked"]}', PLAN)
    for schema in (v13.planner_schema(PLAN), v13.response_schema(PLAN, SNIPPETS, [], parts)):
        response = {'type': 'json_object', 'schema': schema}
        assert _structured_response_format(SplitModel.__new__(SplitModel), response) == response
        assert llama_cpp.LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)


def test_correction_four_thousand_and_forgery():
    parts, _ = v13.decode_plan('{"R1":["What","ready","blocked"]}', PLAN)
    row = {'status': 'supported', 'text': 'A' * 900, 'snippet_ids': ['S1']}
    brief, _, _ = v13.decode_results(json.dumps({'R1': {p['id']: row for p in parts['R1']}}), PLAN, SNIPPETS, [], [], parts)
    correction = deepcopy(brief)
    correction['answers'][0]['text'] = 'A' * 4000
    assert len(v13.corrected_brief(correction, brief, SNIPPETS)['answers'][0]['text']) == 4000
    correction['answers'][0]['text'] += 'A'
    with pytest.raises(ValueError):
        v13.corrected_brief(correction, brief, SNIPPETS)
    forged = deepcopy(brief)
    forged['answers'][0]['source_excerpts'][0]['quote'] = 'forged'
    with pytest.raises(ValueError):
        v13.corrected_brief(forged, brief, SNIPPETS)
    from potluck_local.services import investigation_planned_answers as v12
    old_parts, _ = v12.decode_plan('{"R1":["ready","blocked"]}', PLAN)
    old_row = {'status': 'supported', 'text': 'x' * 600, 'snippet_ids': ['S1']}
    with pytest.raises(ValueError):
        v12.decode_results(json.dumps({'R1': {'R1P1': old_row, 'R1P2': old_row}}), PLAN, SNIPPETS, [], [], old_parts)


def test_invented_spans_fall_back_with_audit_trace():
    proposed = ['procurement receives', 'finance manager approves']
    parts, trace = v13.decode_plan(json.dumps({'R1': proposed}), PLAN)
    assert parts['R1'] == [{'id': 'R1P1', 'request_span': PLAN[0]['request']}]
    assert trace['R1']['proposed_spans'] == proposed
    assert trace['R1']['mode'] == 'original_request_fallback' and trace['R1']['reason']
    assert v13.part_text_limits(parts) == {'R1P1': 1000}
    row = {'status': 'supported', 'text': 'x' * 1000, 'snippet_ids': ['S1']}
    assert len(v13.decode_results(json.dumps({'R1': {'R1P1': row}}), PLAN, SNIPPETS, [], [], parts)[0]['answers'][0]['text']) == 1000
    row['text'] += 'x'
    with pytest.raises(ValueError):
        v13.decode_results(json.dumps({'R1': {'R1P1': row}}), PLAN, SNIPPETS, [], [], parts)


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_native_grammar_compiles_in_subprocess(count):
    import importlib.util
    import os
    from pathlib import Path
    import subprocess
    import sys
    if importlib.util.find_spec('llama_cpp') is None:
        pytest.skip('llama_cpp not installed')
    parts, _ = v13.decode_plan(json.dumps({'R1': ['What', 'is', 'ready', 'blocked'][:count]}), PLAN)
    schema = v13.response_schema(PLAN, SNIPPETS, [], parts)
    script = ('import sys; from llama_cpp import LlamaGrammar, llama_cpp as l; '
              'g=LlamaGrammar.from_json_schema(sys.stdin.read(), verbose=False); '
              'p=l.llama_sampler_init_grammar(None,g._grammar.encode(),b\"root\"); '
              'assert p; l.llama_sampler_free(p)')
    from llama_cpp import llama_cpp
    env = os.environ.copy()
    env['LLAMA_CPP_LIB_PATH'] = str(Path(llama_cpp._lib._name).resolve().parent)
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(schema), text=True, capture_output=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('count,per_part', [(3, 5), (4, 8)])
def test_aggregate_citations_and_correction_roundtrip(count, per_part):
    snippets = [{'snippet_id': f'S{i}', 'citation_id': f'C{i}', 'quote': f'Evidence {i}.'} for i in range(1, 34)]
    parts, _ = v13.decode_plan(json.dumps({'R1': ['What', 'is', 'ready', 'blocked'][:count]}), PLAN)
    values = {p['id']: {'status': 'supported', 'text': 'Evidence.',
                        'snippet_ids': [f'S{i}' for i in range(index * per_part + 1, (index + 1) * per_part + 1)]}
              for index, p in enumerate(parts['R1'])}
    brief, _, _ = v13.decode_results(json.dumps({'R1': values}), PLAN, snippets, [], [], parts)
    assert len(brief['answers'][0]['snippet_ids']) == count * per_part
    corrected = v13.corrected_brief(deepcopy(brief), brief, snippets)
    assert corrected['answers'][0]['source_excerpts'] == brief['answers'][0]['source_excerpts']
    if count == 4:
        excessive = deepcopy(brief)
        excessive['answers'][0]['snippet_ids'].append('S33')
        del excessive['answers'][0]['source_excerpts']
        with pytest.raises(ValueError):
            v13.corrected_brief(excessive, brief, snippets)
    bad = deepcopy(values)
    bad[parts['R1'][0]['id']]['snippet_ids'] = [f'S{i}' for i in range(1, 10)]
    with pytest.raises(ValueError):
        v13.decode_results(json.dumps({'R1': bad}), PLAN, snippets, [], [], parts)
