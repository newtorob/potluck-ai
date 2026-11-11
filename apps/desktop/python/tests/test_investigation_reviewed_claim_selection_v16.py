import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v16 as v
from potluck_local.services import investigation_reviewed_evidence as reviewed

def f(a,b,ids,verdict=None):
    d={'start_token_id':a,'end_token_id':b}
    d.update({'unit_ids':ids} if verdict is None else {'verdict':verdict,'relevant_unit_ids':ids})
    return d

def raw(*facts): return json.dumps({'facts':facts})

def setup():
    e=[{'kind':'reviewed_claim','citation_id':'claim:x','content':'Approval is required.','sources':[{'citation_id':'source:x','content':'Approval is required.'}]}]
    q='In 2020 what rule and expiry date?'
    p=v.prepare(q,e); first=v.decode_selection(raw(f('T4','T4',['C1:whole'])),p)
    return e,q,p,first

def test_known_unknown_and_source_render():
    e,q,p,first=setup()
    reply=raw(f('T4','T4',['C1:whole'],'supported'),f('T6','T7',[],'unknown'))
    d=v.decode_review(reply,p,first)
    assert d['status']=='unknown' and d['facts'][1]['quote']=='expiry date?'
    snippets,_=reviewed.snippets(e)
    brief,trace=v.make_brief(q,{'id':'q','request':q},e,snippets,json.dumps({'selection_raw':first['raw'],'review_raw':reply}))
    assert 'Approval is required.' in brief['answers'][0]['text'] and 'expiry date?' in brief['answers'][0]['text']
    assert trace['review']==d

def test_invalid_and_tampered():
    _,_,p,first=setup()
    for reply in [raw(f('T4','T4',[],'supported')),raw(f('T4','T4',['C1:whole'],'conflicting')),raw(f('T4','T4',[],'unknown'),f('T4','T5',[],'unknown')),raw(f('T6','T4',[],'unknown')),raw(f('T99','T99',[],'unknown')),'{"facts":[],"facts":[]}']:
        with pytest.raises(ValueError): v.decode_review(reply,p,first)
    bad=deepcopy(first);bad['facts'][0]['quote']='altered'
    with pytest.raises(ValueError):v.review_messages(p,bad)

def test_replacement_and_schema_admission():
    from potluck_local.services.runtime import _structured_response_format
    from potluck_local.services.split_native import SplitModel
    _,_,p,first=setup()
    d=v.decode_review(raw(f('T6','T7',[],'unknown')),p,first)
    assert d['unit_ids']==[] and d['dropped_proposal_unit_ids']==['C1:whole']
    for schema in (v.response_schema(p),v.review_schema(p,first)):
        assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':schema})

def test_conflict_and_unknown_shared_unit():
    from potluck_local.services.reviewed_source_unit_plan import text_hash
    e,q,_,_=setup();e[0]['content']='First. Second.';e[0]['sources'][0]['content']=e[0]['content']
    plans={'claim:x':{'source_sha256':text_hash(e[0]['content']),'units':[{'id':'a','ranges':[[0,6]],'required_unit_ids':[]},{'id':'b','ranges':[[7,14]],'required_unit_ids':[]}]}}
    p=v.prepare(q,e,unit_plans=plans); first=v.decode_selection(raw(f('T4','T4',['C1:a'])),p)
    d=v.decode_review(raw(f('T4','T4',['C1:a','C1:b'],'conflicting'),f('T6','T7',['C1:a'],'unknown')),p,first)
    assert d['status']=='conflicting' and d['review_unit_ids']==['C1:a','C1:b']

def test_full_replay_and_tamper():
    e,q,p,first=setup(); snippets,_=reviewed.snippets(e);op={'id':'q','request':q}
    reply=raw(f('T4','T4',['C1:whole'],'supported'),f('T6','T7',[],'unknown'))
    brief,trace=v.make_brief(q,op,e,snippets,json.dumps({'selection_raw':first['raw'],'review_raw':reply}))
    base={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.','messages_sha256':v._hash(v.messages(p)),'schema_sha256':v._hash(v.response_schema(p))}
    ri={'model_visible_claim_ids':['C1'],'support_visibility':base['support_visibility'],'proposal_unit_ids':first['requested_unit_ids'],'proposal_context_unit_ids':first['proposal_context_unit_ids'],'messages_sha256':v._hash(v.review_messages(p,first)),'schema_sha256':v._hash(v.review_schema(p,first)),'input_tokens':100,'output_tokens':512,'context_tokens':8192}
    item={'issue':q,'evidence':e,'brief':brief,'provenance':{'operation_plan':[op],'reviewed_claim_selection':trace,'reviewed_unit_plan_receipts':{'claim:x':None},'reviewed_claim_selection_input':base,'reviewed_claim_review_input':ri,'input_budget':{**base,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}}}
    assert v.validate_brief(item,snippets)==brief
    for field in ('trace','text','budget'):
        bad=deepcopy(item)
        if field=='trace':bad['provenance']['reviewed_claim_selection']['review']['facts'][0]['quote']='changed'
        elif field=='text':bad['brief']['answers'][0]['text']='invented'
        else:bad['provenance']['input_budget']['input_tokens']=True
        with pytest.raises(ValueError):v.validate_brief(bad,snippets)

@pytest.mark.parametrize('facts', [[], [{}] * 7, 'bad', [None],
    [{'start_token_id':'T4','end_token_id':'T4','unit_ids':['C1:whole'],'extra':True}],
    [{'start_token_id':True,'end_token_id':'T4','unit_ids':[]}],
    [{'start_token_id':'T4','end_token_id':'T4','unit_ids':['missing']}],
    [{'start_token_id':'T4','end_token_id':'T4','unit_ids':['C1:whole','C1:whole']}],
    [{'start_token_id':'T6','end_token_id':'T7','unit_ids':[]},
     {'start_token_id':'T4','end_token_id':'T4','unit_ids':[]}],
])
def test_invalid_proposal_inventory_rejected(facts):
    _,_,p,_=setup()
    with pytest.raises(ValueError):
        v.decode_selection(json.dumps({'facts':facts}),p)


def expanded_catalog():
    from potluck_local.services.reviewed_source_unit_plan import text_hash
    text=''.join(f'Rule {i}.\n' for i in range(18))
    e=[{'kind':'reviewed_claim','citation_id':'claim:x','content':text,
        'sources':[{'citation_id':'source:x','content':text}]}]
    starts=[text.index(f'Rule {i}.') for i in range(18)]+[len(text)]
    plan={'source_sha256':text_hash(text),'units':[
        {'id':f'u{i:02}','ranges':[[starts[i],starts[i+1]]],
         'required_unit_ids':[f'u{j:02}' for j in range(1,17)] if i==0 else []}
        for i in range(18)]}
    return v.prepare('Which rule and which date?',e,unit_plans={'claim:x':plan})


def test_aggregate_proposal_ids_remain_bounded_but_closure_can_be_replaced():
    p=expanded_catalog()
    first=v.decode_selection(raw(f('T1','T2',['C1:u00'])),p)
    assert len(first['proposal_context_unit_ids'])==17
    d=v.decode_review(raw(f('T1','T2',['C1:u17'],'supported')),p,first)
    assert d['unit_ids']==['C1:u17']
    groups=[[f'C1:u{i:02}' for i in range(9)],[f'C1:u{i:02}' for i in range(9,18)]]
    with pytest.raises(ValueError,match='unit IDs'):
        v.decode_selection(raw(f('T1','T2',groups[0]),f('T4','T5',groups[1])),p)
    with pytest.raises(ValueError,match='closed unit'):
        v.decode_review(raw(f('T1','T2',groups[0],'supported'),f('T4','T5',groups[1],'supported')),p,first)


def test_conflict_and_unknown_render_distinct_scopes_with_single_source_copy():
    from potluck_local.services.reviewed_source_unit_plan import text_hash
    text='Default is 4 days. Default is 8 days.'
    e=[{'kind':'reviewed_claim','citation_id':'claim:x','content':text,
        'sources':[{'citation_id':'source:x','content':text}]}]
    boundary=text.index('Default is 8')
    plans={'claim:x':{'source_sha256':text_hash(text),'units':[
        {'id':'a','ranges':[[0,boundary]],'required_unit_ids':[]},
        {'id':'b','ranges':[[boundary,len(text)]],'required_unit_ids':[]}]}}
    q='What default and exact expiry date?';p=v.prepare(q,e,unit_plans=plans)
    first=v.decode_selection(raw(f('T1','T2',['C1:a'])),p)
    reply=raw(f('T1','T2',['C1:a','C1:b'],'conflicting'),f('T4','T6',['C1:a'],'unknown'))
    snippets,_=reviewed.snippets(e)
    brief,_=v.make_brief(q,{'id':'q','request':q},e,snippets,
                        json.dumps({'selection_raw':first['raw'],'review_raw':reply}),unit_plans=plans)
    a=brief['answers'][0]
    assert a['status']=='conflicting'
    assert 'conflict on: What default' in a['text']
    assert 'no authoritative answer' in a['text']
    assert 'do not specify: exact expiry date?' in a['text']
    assert a['text'].count('Default is 4 days.')==1
    assert len(a['passage_presentation']['passages'])==2


def test_explicit_facts_do_not_turn_source_framing_into_rendered_uncertainty():
    e,q,p,first=setup()
    reply=raw(f('T4','T4',['C1:whole'],'supported'))
    snippets,_=reviewed.snippets(e)
    brief,_=v.make_brief(q,{'id':'q','request':q},e,snippets,
                        json.dumps({'selection_raw':first['raw'],'review_raw':reply}))
    assert brief['answers'][0]['status']=='supported'
    assert brief['answers'][0]['text']=='Approval is required.'
    # Declared spans are not semantic proof; actual requested dates remain allowed.
    d=v.decode_review(raw(f('T2','T2',[],'unknown')),p,first)
    assert d['facts'][0]['quote']=='2020'


@pytest.mark.parametrize('mode', ['supported','conflicting','unknown'])
def test_final_fact_inventory_and_ids_reject_duplicate_or_unknown_fields(mode):
    _,_,p,first=setup()
    fact=f('T4','T4',['C1:whole'],mode)
    for changed in [{**fact,'unexpected':'x'},{**fact,'relevant_unit_ids':['C1:whole','C1:whole']},
                    {**fact,'relevant_unit_ids':['C1:no-such-unit']},{**fact,'verdict':True}]:
        with pytest.raises(ValueError):v.decode_review(raw(changed),p,first)
