import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_aspect_source_answer as v

P = [{'id':'R1','question_id':'Q1','request':'Explain all.','operation':'answer'}]
S = [{'snippet_id':'S1','citation_id':'c1','quote':'First. '}, {'snippet_id':'S2','citation_id':'c2','quote':'Second.\n'}]
def run(groups, more=False):
    return v.decode_results(json.dumps({'R1':{'groups':groups,'more':more}}), P, S, [], [])
def g(focus, ids, status='supported'):
    return {'focus':focus,'snippet_ids':ids,'status':status}

def test_union_status_and_original_groups():
    brief, _, trace = run([g('First focus',['S1']),g('Second focus',['S1','S2'])])
    assert brief['answers'][0]['snippet_ids'] == ['S1','S2']
    assert brief['answers'][0]['text'].count('[S1]') == 1
    assert brief['answers'][0]['text'].endswith('Second.\n')
    assert trace['focus_groups']['R1']['groups'][1]['snippet_ids'] == ['S1','S2']
    assert brief['answers'][0]['status'] == 'supported'
    correction = v.corrected_brief(deepcopy(brief),brief,S)
    assert correction['answers'][0]['source_excerpts'] == brief['answers'][0]['source_excerpts']

def test_status_and_more_separate():
    brief, _, trace = run([g('One', ['S1']), g('Missing', [], 'unknown')])
    assert brief['answers'][0]['status'] == 'unknown'
    brief, _, trace = run([g('One', ['S1']),g('Disagreement',['S2'],'conflicting')], True)
    assert brief['answers'][0]['status'] == 'incomplete'
    assert brief['answers'][0]['model_status'] == 'conflicting'
    assert trace['focus_groups']['R1']['aggregate_status'] == 'conflicting'

def test_invalid_and_capacity():
    for groups in ([g('Missing', [], 'supported')], [g('x'*97,['S1'])], [g('x',['bad'])], [g('x',['S1','S1'])]):
        with pytest.raises(ValueError): run(groups)
    with pytest.raises(ValueError): v.group_limits([{'id':str(i),'operation':'answer'} for i in range(7)])
    with pytest.raises(ValueError): run([g(str(i),['S1']) for i in range(7)])

def test_limits_count_displayed_passages_per_operation_and_text():
    plan = P + [{'id':'R2','question_id':'Q2','request':'Explain too.','operation':'answer'}]
    raw = {'R1':{'groups':[g('One',['S1'])],'more':False}, 'R2':{'groups':[g('Two',['S1'])],'more':False}}
    brief, _, _ = v.decode_results(json.dumps(raw), plan, S, [], [])
    assert all(a['snippet_ids'] == ['S1'] for a in brief['answers'])
    oversized = [{'snippet_id':'S1','citation_id':'c1','quote':'a'*4000}]
    with pytest.raises(ValueError): v.decode_results(json.dumps({'R1':raw['R1']}), P, oversized, [], [])

def test_strict_json_and_duplicate_focus():
    with pytest.raises(ValueError): run([g('Same',['S1']), g('Same',['S2'])])
    with pytest.raises(ValueError): v.decode_results('{"R1":{},"R1":{}}', P, S, [], [])
    with pytest.raises(ValueError): run([dict(g('One',['S1']), extra='x')])

def test_more_correction_requires_real_edit_preserves_original():
    brief, _, _ = run([g('One',['S1'])], True)
    before = json.dumps(brief)
    change = deepcopy(brief)
    change['answers'][0]['status'] = 'supported'
    with pytest.raises(ValueError): v.corrected_brief(change, brief, S)
    change['answers'][0]['text'] = 'Owner correction.'
    result = v.corrected_brief(change, brief, S)
    assert result['answers'][0]['status'] == 'supported'
    assert result['model_uncertainties'] == [v.WARNING]
    assert json.dumps(brief) == before
