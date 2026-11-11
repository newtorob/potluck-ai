import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_aspect_source_answer_v20 as v

P=[{'id':'R1','request':'Explain','operation':'answer'}]
def decode(count, length=10, more=False):
    snippets=[{'snippet_id':f'S{i}', 'citation_id':f'c{i}', 'quote':'x'*length} for i in range(count)]
    groups=[{'focus':str(i//8),'snippet_ids':[f'S{j}' for j in range(i,min(i+8,count))], 'status':'supported'} for i in range(0,count,8)]
    brief, _, _=v.decode_results(json.dumps({'R1':{'groups':groups,'more':more}}),P,snippets,[],[])
    return brief,snippets

def test_count_and_correction():
    brief,snips=decode(16,300,True)
    assert len(brief['answers'][0]['text']) > 4000
    assert len(brief['answers'][0]['snippet_ids']) == 16
    no_op=deepcopy(brief); no_op['answers'][0]['status']='supported'
    with pytest.raises(ValueError): v.corrected_brief(no_op,brief,snips)
    fixed=deepcopy(brief); fixed['answers'][0]['status']='supported'; fixed['answers'][0]['text'] += ' changed'
    assert v.corrected_brief(fixed,brief,snips)['answers'][0]['status']=='supported'
    with pytest.raises(ValueError): decode(17)

def test_text_exact_boundary():
    brief,_=decode(8,1)
    framing=len(brief['answers'][0]['text'])-8
    sizes=[(8000-framing)//8]*8
    for i in range((8000-framing)%8): sizes[i]+=1
    snips=[{'snippet_id':f'S{i}','citation_id':f'c{i}','quote':'x'*sizes[i]} for i in range(8)]
    raw={'R1':{'groups':[{'focus':'All','snippet_ids':[f'S{i}' for i in range(8)],'status':'supported'}],'more':False}}
    result,_,_=v.decode_results(json.dumps(raw),P,snips,[],[])
    assert len(result['answers'][0]['text']) == 8000
    snips[0]['quote'] += 'x'
    with pytest.raises(ValueError): v.decode_results(json.dumps(raw),P,snips,[],[])

def test_group_limit_is_eight():
    assert v.response_schema(P,[],[])['properties']['R1']['properties']['groups']['items']['properties']['snippet_ids']['maxItems']==8

def test_displayed_count_across_operations():
    plans=[{'id':'R1','request':'First','operation':'answer'},{'id':'R2','request':'Second','operation':'answer'}]
    snippets=[{'snippet_id':f'S{i}','citation_id':f'c{i}','quote':'text'} for i in range(9)]
    def record(ids): return {'groups':[{'focus':'focus','snippet_ids':ids,'status':'supported'}],'more':False}
    raw={'R1':record([f'S{i}' for i in range(8)]),'R2':record([f'S{i}' for i in range(8)])}
    brief,_,_=v.decode_results(json.dumps(raw),plans,snippets,[],[])
    assert sum(len(a['snippet_ids']) for a in brief['answers']) == 16
    raw['R2']['groups'].append({'focus':'extra','snippet_ids':['S8'],'status':'supported'})
    with pytest.raises(ValueError): v.decode_results(json.dumps(raw),plans,snippets,[],[])

def test_correction_limit_and_legacy_unchanged():
    from potluck_local.services import investigation_aspect_source_answer as v19
    brief,snips=decode(16,300)
    with pytest.raises(ValueError): v19.corrected_brief(deepcopy(brief),brief,snips)
    corrected=deepcopy(brief); corrected['answers'][0]['text']='x'*8001
    with pytest.raises(ValueError): v.corrected_brief(corrected,brief,snips)
    corrected['answers'][0]['text']='x'*8000
    assert len(v.corrected_brief(corrected,brief,snips)['answers'][0]['text']) == 8000

def test_incomplete_normalized_clear_and_original_metadata_tampering():
    brief,snips=decode(2,10,True)
    for suffix in ('', '  \n'):
        candidate=deepcopy(brief); candidate['answers'][0]['status']='supported'; candidate['answers'][0]['text'] += suffix
        with pytest.raises(ValueError): v.corrected_brief(candidate,brief,snips)
    for key in ('parts','source_excerpts'):
        candidate=deepcopy(brief); candidate['answers'][0][key][0]['text' if key == 'parts' else 'quote']='tampered'
        with pytest.raises(ValueError): v.corrected_brief(candidate,brief,snips)

def test_redaction_normalized_clear_rejected():
    from potluck_local.services.memory_safety import redact_credentials
    secret='sk-proj-'+'Synthetic09_'*4
    redacted=redact_credentials(secret)
    assert redacted != secret
    brief,snips=decode(1,10,True)
    brief['answers'][0]['text'] += ' '+redacted
    candidate=deepcopy(brief); candidate['answers'][0]['status']='supported'
    candidate['answers'][0]['text']=candidate['answers'][0]['text'].replace(redacted,secret)
    with pytest.raises(ValueError): v.corrected_brief(candidate,brief,snips)
