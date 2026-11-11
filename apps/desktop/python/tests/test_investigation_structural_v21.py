import json
import pytest
from potluck_local.services import investigation_context as context, investigation_structural_context as structural, investigation_structural_answer as answer

def sample(text, full=None):
    evidence=[{'kind':'source_chunk','citation_id':'c','content':full or text,'presented_content':text}]
    snippets,_=context.snippets_from_evidence(evidence,version='paragraph-v3')
    return evidence,snippets,structural.catalog(evidence,snippets)

def test_closure_and_correction():
    e,s,t=sample('Intro\n\n1. one\n2. two\n3. three\n4. four\n\n# heading\n')
    selected=t['blocks'][0]['snippet_ids'][-1]
    raw={'R1':{'groups':[{'focus':'procedure','snippet_ids':[selected],'status':'supported'}],'more':False}}
    b,_,trace=answer.decode_results(json.dumps(raw),[{'id':'R1','request':'Explain','operation':'answer'}],s,[],[],structural_trace=t)
    assert len(b['answers'][0]['snippet_ids'])==4
    assert len(trace['structural_context']['operations']['R1']['server_added_ids'])==3
    assert 'server-added structural context' in b['answers'][0]['text']
    assert answer.corrected_brief(b,b,s)['answers'][0]['status']=='supported'

def test_fence_nested_restart_and_truncation():
    e,s,t=sample('```\n1. no\n2. no\n```\n\n1. one\n  1. nested\n2. two\n\n1. restart\n2. other\n\n# End\n')
    assert len(t['blocks'])==2 and all(not b['complete'] for b in t['blocks'])
    e,s,t=sample('1. one\n2. two\n', '1. one\n2. two\n3. more\n')
    assert not t['blocks'][0]['complete']

def test_long_item_and_budget():
    e,s,t=sample('1. '+('a '*650)+'\n2. two\n\n# End\n')
    assert t['blocks'][0]['complete'] and len(t['blocks'][0]['snippet_ids'])>=3
    text=''.join(f'{i}. '+('x'*600)+'\n' for i in range(1,17))+'\n# End\n'
    e,s,t=sample(text); selected=t['blocks'][0]['snippet_ids'][0]
    raw={'R1':{'groups':[{'focus':'procedure','snippet_ids':[selected],'status':'supported'}],'more':False}}
    with pytest.raises(ValueError): answer.decode_results(json.dumps(raw),[{'id':'R1','request':'Explain','operation':'answer'}],s,[],[],structural_trace=t)


def test_truncated_selected_visible_limitation_and_separate_citations():
    e,s,t=sample('1. one\n2. two\n', '1. one\n2. two\n3. more\n')
    raw={'R1':{'groups':[{'focus':'procedure','snippet_ids':[t['blocks'][0]['snippet_ids'][0]],'status':'supported'}],'more':False}}
    b,_,trace=answer.decode_results(json.dumps(raw),[{'id':'R1','request':'Explain','operation':'answer'}],s,[],[],structural_trace=t)
    assert 'Structural context may be incomplete' in b['answers'][0]['text']
    assert trace['structural_context']['operations']['R1']['incomplete']
    e=[{'kind':'source_chunk','citation_id':'a','content':'1. a\n','presented_content':'1. a\n'}, {'kind':'source_chunk','citation_id':'b','content':'2. b\n','presented_content':'2. b\n'}]
    s,_=context.snippets_from_evidence(e,version='paragraph-v3')
    assert not structural.catalog(e,s)['blocks'][0]['complete']
    assert all(len(block['snippet_ids']) == 1 for block in structural.catalog(e,s)['blocks'])

def test_adjacent_gap_and_restart_both_sides_uncertain():
    for sequence in ('1. one\n2. two\n4. four\n5. five\n', '1. one\n2. two\n1. restart\n2. two\n'):
        e,s,t=sample('Intro\n\n'+sequence+'\nEnd.\n')
        assert len(t['blocks']) == 2 and all(not b['complete'] for b in t['blocks'])

@pytest.mark.parametrize('operation,roles,expected',[
    ('difference', {'minuend_id':'N2','subtrahend_id':'N1'}, ('minuend: 3.75 %','subtrahend: 5 %')),
    ('percentage_point_difference', {'minuend_id':'N2','subtrahend_id':'N1'}, ('minuend: 3.75 %','subtrahend: 5 %')),
    ('percent_change', {'baseline_id':'N1','current_id':'N2'}, ('baseline: 5 %','current: 3.75 %')),
    ('percentage', {'part_id':'N2','total_id':'N1'}, ('part: 3.75 %','total: 5 %')),
])
def test_calculation_visible_inputs_and_expression(operation,roles,expected):
    from potluck_local.services.investigation_calculations import quantity_catalog
    snippets=[{'snippet_id':'S1','citation_id':'C1','quote':'Before 5%.'},{'snippet_id':'S2','citation_id':'C2','quote':'After 3.75%.'}]
    quantities=quantity_catalog(snippets,version='v4')
    plan=[{'id':'R1','request':'Compute','operation':operation}]
    item={'status':'ready','reason':'Cited inputs.',**roles,'known_input_snippet_ids':['S1','S2'],'competing_snippet_ids':[],'limitation_snippet_ids':[],'limitation_basis':'none'}
    brief,traces,_=answer.decode_results(json.dumps({'R1':item}),plan,snippets,quantities,[],structural_trace={'blocks':[]})
    text=brief['answers'][0]['text']
    assert all(value in text for value in expected)
    assert traces[0]['evaluated_expression'] in text and traces[0]['status']=='computed'
    assert answer.corrected_brief(brief,brief,snippets)['answers'][0]['text']==text

@pytest.mark.parametrize('status', ['missing','unsupported'])
def test_unavailable_calculation_not_projected(status):
    snippets=[{'snippet_id':'S1','citation_id':'C1','quote':'No quantity.'}]
    item={'status':status,'reason':'Not established.','minuend_id':'unavailable','subtrahend_id':'unavailable','known_input_snippet_ids':[],'competing_snippet_ids':[],'limitation_snippet_ids':['S1'],'limitation_basis':'documented'}
    brief,_,_=answer.decode_results(json.dumps({'R1':item}),[{'id':'R1','request':'Compute','operation':'difference'}],snippets,[],[],structural_trace={'blocks':[]})
    assert 'Expression:' not in brief['answers'][0]['text'] and 'Inputs:' not in brief['answers'][0]['text']


def test_blank_separated_gap_and_fence_do_not_close_prior_list():
    texts = ('Start.\n\n1. One.\n2. Two.\n\n4. Four.\n\nEnd.',
             'Start.\n\n1. One.\n2. Two.\n   ```\n   code\n   ```\n3. Three.\n\nEnd.')
    for text in texts:
        _, _, trace = sample(text)
        assert trace['blocks'] and all(not b['complete'] for b in trace['blocks'])
