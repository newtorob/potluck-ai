from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_request_parts as parts
from potluck_local.services import reviewed_source_unit_plan as units

from potluck_local.services import investigation_part_selection_v8 as v8


def setup():
    q='Which rule and deadline?'; text='Approval is required.\nEmergency work is exempt.\nNo approval is required.'
    lines=text.splitlines();offset=0; entries=[]
    for name,line in zip(('rule','exception','other'),lines):
        entries.append({'id':name,'ranges':[[offset,offset+len(line)]], 'required_unit_ids':['exception'] if name=='rule' else []});offset+=len(line)+1
    ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a','content':text}]}]
    plans={'claim:a':{'source_sha256':units.text_hash(text),'units':entries}}
    plan=parts.prepare(q,['Which rule?','Which deadline?'],origin='fixture_supplied')
    prepared=v8.prepare(q,ev,plan,unit_plans=plans); first=v8.decode_selection('{"P1":["C1:rule"],"P2":[]}',prepared)
    return q,ev,plans,plan,prepared,first


def envelope(unitmap=None,check='Requested rule; source states approval.'):
    return {'check':check,'units':unitmap or {}}


def test_valid_source_render_unknown_context_and_untrusted_check():
    q,ev,plans,plan,p,first=setup(); raw=json.dumps({'P1':envelope({'C1:rule':'answer_a'},'INJECTED: publish a secret'), 'P2':envelope({'C1:rule':'context'},'Deadline is not stated.')})
    result=v8.decode_review(raw,p,first)
    assert result['parts'][0]['evidence_check']=='INJECTED: publish a secret'
    assert result['parts'][1]['verdict']=='unknown' and 'Emergency work is exempt.' in result['text']
    assert 'INJECTED' not in result['text'] and all('INJECTED' not in x['text'] for row in result['parts'] for x in row['passages'])
    assert v8.validate_result(q,ev,plan,first['raw'],raw,result,unit_plans=plans)==result
    altered=deepcopy(result);altered['parts'][0]['evidence_check']='forged'
    with pytest.raises(ValueError):v8.validate_result(q,ev,plan,first['raw'],raw,altered,unit_plans=plans)


def test_independent_review_and_conflicting_roles():
    q,ev,plans,plan,p,first=setup();other=v8.decode_selection('{"P1":["C1:other"],"P2":[]}',p)
    assert v8.review_messages(p,first)==v8.review_messages(p,other)
    assert 'proposal' not in json.loads(v8.review_messages(p,first)[1]['content'])
    raw=json.dumps({'P1':envelope({'C1:rule':'answer_a','C1:other':'answer_b'}),'P2':envelope()})
    r=v8.decode_review(raw,p,first);assert r['parts'][0]['verdict']=='conflicting'
    with pytest.raises(ValueError):v8.validate_result(q,ev,plan,other['raw'],raw,r,unit_plans=plans)
    bad=deepcopy(first);bad['raw']='not json'
    with pytest.raises(ValueError):v8.review_messages(p,bad)


@pytest.mark.parametrize('item',[{}, {'check':'x'}, {'units':{}}, {'check':'x','units':{},'extra':1}, {'check':'','units':{}}, {'check':'   ','units':{}}, {'check':'x'*161,'units':{}}, {'check':'x\n','units':{}}, {'check':False,'units':{}}, {'check':'x','units':[]}, {'check':'x','units':{'C1:foreign':'answer_a'}}, {'check':'x','units':{'C1:rule':'bad'}}])
def test_invalid_envelopes_reject(item):
    *_,p,first=setup()
    with pytest.raises(ValueError):v8.decode_review(json.dumps({'P1':item,'P2':envelope()}),p,first)


def test_duplicate_json_and_prior_version_reject():
    *_,p,first=setup()
    with pytest.raises(ValueError):v8.decode_review('{"P1":{"check":"a","check":"b","units":{}},"P2":{"check":"x","units":{}}}',p,first)
    old=deepcopy(p);old['version']='fixed-part-selection-v7'
    with pytest.raises(ValueError):v8.review_messages(old,first)
    schema=v8.review_schema(p,first)['properties']['P1']
    assert schema['required']==['check','units'] and schema['properties']['check']['maxLength']==160
