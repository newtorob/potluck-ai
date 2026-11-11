from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v14 as contract


def selection(prepared, assignments=None):
    assignments=assignments or {}
    raw=json.dumps({'inventory':[assignments.get(u['id'],[]) for u in prepared['catalog']['units']]})
    return contract.decode_selection(raw,prepared)


def setup():
    q='Which rule and deadline?'; text='Approval is required.\nEmergency work is exempt.\nNo approval is required.'
    lines=text.splitlines();offset=0; entries=[]
    for name,line in zip(('rule','exception','other'),lines):
        entries.append({'id':name,'ranges':[[offset,offset+len(line)]], 'required_unit_ids':['exception'] if name=='rule' else []});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    plan=parts.prepare(q,['Which rule?','Which deadline?'],origin='fixture_supplied')
    prepared=contract.prepare(q,ev,plan,unit_plans=plans); first=selection(prepared,{'C1:rule':['P1']})
    return q,ev,plans,plan,prepared,first


def envelope(unitmap=None,check='Requested rule; source states approval.'):
    return {'check':check,'units':unitmap or {}}




def item(answer=None,alternative=None,context=None,check='Source supports rule.'):
    return {'verdict':'conflicting' if answer and alternative else 'supported' if answer else 'unknown','check':check,'answer':answer or [],'alternative':alternative or [],'context':context or []}


def wire(p1=None,p2=None,context=None):
    return json.dumps({'parts':{'P1':p1 or item(),'P2':p2 or item()},'context':context or []})


def test_supported_unknown_context_closure_and_replay():
    q,ev,plans,plan,p,first=setup();raw=wire(item(answer=['C1:rule'],check='INJECTED SECRET'),item(context=['C1:rule']),['C1:other'])
    r=contract.decode_review(raw,p,first)
    assert [row['verdict'] for row in r['parts']]==['supported','unknown'] and r['status']=='unknown'
    assert 'Emergency work is exempt.' in r['text'] and 'INJECTED' not in r['text']
    assert r['request_context']['visible_unit_ids']==['C1:other']
    assert contract.validate_result(q,ev,plan,first['raw'],raw,r,unit_plans=plans)==r
    bad=deepcopy(r);bad['parts'][0]['evidence_check']='forged'
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,first['raw'],raw,bad,unit_plans=plans)


def test_conflict_and_alternative_only_rejected():
    *_,p,first=setup()
    conflict=contract.decode_review(wire(item(answer=['C1:rule'],alternative=['C1:other'])),p,first)
    assert conflict['parts'][0]['verdict']=='conflicting' and 'no authoritative winner' in conflict['text'].lower()
    with pytest.raises(ValueError):contract.decode_review(wire(item(alternative=['C1:other'])),p,first)


@pytest.mark.parametrize('bad', [
    {}, {'check':'x','answer':[],'alternative':[]},
    {'check':'x','answer':[],'alternative':[],'context':[],'extra':1},
    item(answer=['C1:rule','C1:rule']), item(answer=['C1:rule'],context=['C1:rule']),
    item(answer=['C1:foreign']), item(answer=[True]), item(check='x'*81), item(check='x\n')])
def test_invalid_envelopes_or_groups_fail_closed(bad):
    *_,p,first=setup()
    with pytest.raises(ValueError):contract.decode_review(json.dumps({'parts':{'P1':bad,'P2':item()},'context':[]}),p,first)


def test_duplicates_first_and_replay_binding():
    q,ev,plans,plan,p,first=setup();other=selection(p,{'C1:other':['P1']});raw=wire(item(answer=['C1:other']))
    with pytest.raises(ValueError):contract.decode_review('{"parts":{"P1":{"check":"x","answer":[],"answer":[],"alternative":[],"context":[]},"P2":{}},"context":[]}',p,first)
    r=contract.decode_review(raw,p,first)
    with pytest.raises(ValueError):contract.validate_result(q,ev,plan,other['raw'],raw,r,unit_plans=plans)
    old=deepcopy(p);old['version']='fixed-part-selection-v12'
    with pytest.raises(ValueError):contract.review_messages(old,first)
    assert list(contract.review_schema(p,first)['properties'])==['parts','context']


def test_global_union_bounds_and_inventory_are_independent():
    q='What applies?';lines=[f'Rule {i}.' for i in range(72)];text='\n'.join(lines);offset=0;entries=[]
    for i,line in enumerate(lines):
        entries.append({'id':f'u{i:02}','ranges':[[offset,offset+len(line)]],'required_unit_ids':[]});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plan=parts.prepare(q,[q],origin='fixture_supplied');plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    p=contract.prepare(q,ev,plan,unit_plans=plans);first=selection(p,{f'C1:u{i:02}':['P1'] for i in range(72)})
    assert len(first['inventory'])==72
    raw=json.dumps({'parts':{'P1':item(answer=['C1:u71'])},'context':[]})
    assert contract.decode_review(raw,p,first)['unit_ids']==['C1:u71']
    overflow=json.dumps({'parts':{'P1':item(answer=[f'C1:u{i:02}' for i in range(9)])},'context':[f'C1:u{i:02}' for i in range(9,17)]})
    with pytest.raises(ValueError):contract.decode_review(overflow,p,first)


def test_compact_inventory_order_schema_and_raw_boundaries():
    _,_,_,_,prepared,first=setup()
    payload=json.loads(contract.messages(prepared)[1]['content'])
    ids=[u['id'] for u in prepared['catalog']['units']]
    assert payload['inventory_unit_order']==ids
    schema=contract.response_schema(prepared)['properties']['inventory']
    assert schema['minItems']==schema['maxItems']==len(ids)
    for bad in ([], [[],[],[],[]], [[],True,[]], [['P1','P1'],[],[]], [['P9'],[],[]]):
        with pytest.raises(ValueError):contract.decode_selection(json.dumps({'inventory':bad}),prepared)
    with pytest.raises(ValueError):contract.decode_selection(json.dumps({uid:[] for uid in ids}),prepared)
    with pytest.raises(ValueError):contract.decode_selection('{"inventory":[],"inventory":[]}',prepared)
    bad=deepcopy(prepared);bad['catalog']['units'].reverse()
    with pytest.raises(ValueError):contract.review_messages(bad,first)
    altered=deepcopy(first);altered['inventory']={k:altered['inventory'][k] for k in reversed(ids)}
    # Mapping order is metadata; altering which ID receives a label must fail replay.
    altered['inventory'][ids[0]]=[]
    with pytest.raises(ValueError):contract.review_messages(prepared,altered)


def test_first_message_order_is_exact_bound_catalog_not_source_instructions():
    _,_,_,_,prepared,_=setup()
    message=json.loads(contract.messages(prepared)[1]['content'])
    assert message['inventory_unit_order']==[u['id'] for u in prepared['catalog']['units']]
    assert 'inventory_unit_order' not in contract.review_messages(prepared,selection(prepared))[1]


@pytest.mark.parametrize('verdict,answer,alternative', [('supported',[],[]),('unknown',['C1:rule'],[]),('conflicting',['C1:rule'],[]),('conflicting',[],['C1:other']),('invalid',[],[]),(True,[],[])])
def test_declared_verdict_must_match_groups(verdict,answer,alternative):
    *_,p,first=setup()
    bad=item(answer=answer,alternative=alternative);bad['verdict']=verdict
    with pytest.raises(ValueError):contract.decode_review(wire(bad),p,first)

def test_review_schema_compiles_with_local_llama_grammar():
    from llama_cpp import LlamaGrammar
    *_,p,first=setup()
    grammar=LlamaGrammar.from_json_schema(json.dumps(contract.review_schema(p,first)),verbose=False)
    assert grammar is not None


@pytest.mark.parametrize('declared,answers,alternatives', [
    ('unknown', [], []), ('supported', ['C1:rule'], []),
    ('conflicting', ['C1:rule'], ['C1:other'])])
def test_schema_and_decoder_agree_on_canonical_verdict_groups(declared, answers, alternatives):
    import jsonschema
    *_, prepared, first = setup()
    raw = wire(item(answer=answers, alternative=alternatives))
    jsonschema.validate(json.loads(raw), contract.review_schema(prepared, first))
    assert contract.decode_review(raw, prepared, first)['parts'][0]['verdict'] == declared
    for wrong in {'unknown', 'supported', 'conflicting'} - {declared}:
        altered = json.loads(raw)
        altered['parts']['P1']['verdict'] = wrong
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(altered, contract.review_schema(prepared, first))
        with pytest.raises(ValueError):
            contract.decode_review(json.dumps(altered), prepared, first)


def test_confident_check_cannot_supply_absent_answer_evidence():
    *_, prepared, first = setup()
    raw = wire(item(check='All approvals are granted.'), item(check='Deadline is seven days.'))
    result = contract.decode_review(raw, prepared, first)
    assert result['status'] == 'unknown' and result['unit_ids'] == []
    assert 'seven days' not in result['text'] and 'approvals are granted' not in result['text']
    assert all(row['verdict'] == 'unknown' and row['passages'] == [] for row in result['parts'])


def test_original_ordered_inventory_and_review_are_not_mutated_by_decoder():
    *_, prepared, first = setup()
    original_prepared, original_first = deepcopy(prepared), deepcopy(first)
    result = contract.decode_review(wire(item(answer=['C1:rule'])), prepared, first)
    result['parts'][0]['answer_unit_ids'].clear()
    result['parts'][0]['passages'][0]['ranges'][0][0] = 999
    assert prepared == original_prepared and first == original_first
