import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v7 as v7
from potluck_local.services import investigation_reviewed_evidence as reviewed
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel

def evidence(text='## Policy\n- Approval is required.\n- Suspended members are excluded.'):
 return [{'kind':'reviewed_claim','citation_id':'claim:one','content':text,'sources':[{'citation_id':'source:one:rev','content':text}]}]
def enc(x):return json.dumps(x)
def test_fixed_stages_retention_and_schema():
 p=v7.prepare('Who may apply?',evidence());first=v7.decode_selection(enc({'unit_ids':['U2']}),p)
 assert 'U2' in first['unit_ids'] and 'U1' in first['unit_ids']
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v7.response_schema(p)})
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v7.review_schema(p,first)})
 state=v7.decode_review(enc({'add_unit_ids':['U3','U2'],'verdict':'supported'}),p,first)
 assert set(first['unit_ids'])<=set(state['unit_ids']) and 'U3' in state['unit_ids']
 payload=json.loads(v7.review_messages(p,first)[1]['content']);assert payload['retained_unit_ids']==first['unit_ids'] and len(payload['claims'][0]['units'])==3

def test_strict_first_and_review_rejection():
 p=v7.prepare('What time?',evidence());first=v7.decode_selection(enc({'unit_ids':['U2']}),p)
 for raw in ['{"unit_ids":["U2"],"status":"supported"}','{"unit_ids":["U2"],"unit_ids":[]}',enc({'unit_ids':['U2','U2']}),enc({'unit_ids':['U99']})]:
  with pytest.raises(ValueError):v7.decode_selection(raw,p)
 for data in [{'add_unit_ids':[],'verdict':'supported: x'},{'add_unit_ids':[],'verdict':'unknown: made up'},{'add_unit_ids':['U99'],'verdict':'supported'},{'add_unit_ids':['U3','U3'],'verdict':'supported'},{'add_unit_ids':[],'verdict':'supported','text':'injected'}]:
  with pytest.raises(ValueError):v7.decode_review(enc(data),p,first)
 forged=deepcopy(first);forged['unit_ids']=[]
 with pytest.raises(ValueError):v7.review_messages(p,forged)

def test_bundle_replay_and_renderer_identity():
 e=evidence();s,_=reviewed.snippets(e);q='Who may apply?';op={'id':'q','request':q};first=enc({'unit_ids':['U2']});review=enc({'add_unit_ids':['U3'],'verdict':'supported'});bundle=enc({'selection_raw':first,'review_raw':review})
 brief,trace=v7.make_brief(q,op,e,s,bundle)
 assert trace['version']==v7.VERSION and trace['renderer_version']=='reviewed-claim-selection-v6'
 assert trace['selection']['raw']==first and trace['review']['raw']==review
 assert 'Approval is required' in brief['answers'][0]['text'] and 'Suspended members' in brief['answers'][0]['text']
 assert brief['answers'][0]['passage_presentation']['version']=='reviewed-claim-selection-v6'
 p=v7.prepare(q,e,q);state=v7.decode_selection(first,p);h=v7._digest
 first_input={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.','messages_sha256':h(v7.messages(p)),'schema_sha256':h(v7.response_schema(p))}
 review_input={'model_visible_claim_ids':['C1'],'support_visibility':first_input['support_visibility'],'retained_unit_ids':state['unit_ids'],'messages_sha256':h(v7.review_messages(p,state)),'schema_sha256':h(v7.review_schema(p,state)),'input_tokens':100,'output_tokens':512,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first_input,'reviewed_claim_review_input':review_input,'input_budget':{**first_input,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192},'operation_plan':[op]}}
 assert v7.validate_brief(item,s)==brief
 for key in ('messages_sha256','schema_sha256'):
  broken=deepcopy(item);broken['provenance']['reviewed_claim_review_input'][key]='bad'
  with pytest.raises(ValueError):v7.validate_brief(broken,s)
 broken=deepcopy(item);broken['brief']['answers'][0]['text']='changed'
 with pytest.raises(ValueError):v7.validate_brief(broken,s)
 with pytest.raises(ValueError):v7.make_brief(q,op,e,s,enc({'selection_raw':first,'review_raw':review,'extra':1}))

def test_unknown_uses_operation_request_only():
 p=v7.prepare('Broader issue mentions secret deadline',evidence(),'What is the date?');first=v7.decode_selection(enc({'unit_ids':[]}),p)
 with pytest.raises(ValueError):v7.decode_review(enc({'add_unit_ids':[],'verdict':'unknown: secret deadline'}),p,first)
 assert v7.decode_review(enc({'add_unit_ids':[],'verdict':'unknown: date'}),p,first)['missing_request_quote']=='date'

def test_review_budget_and_retained_trace_tamper():
 e=evidence();snips,_=reviewed.snippets(e);q='Who may apply?';op={'id':'q','request':q};one=enc({'unit_ids':['U2']});two=enc({'add_unit_ids':['U3'],'verdict':'supported'});bundle=enc({'selection_raw':one,'review_raw':two});brief,trace=v7.make_brief(q,op,e,snips,bundle)
 p=v7.prepare(q,e,q);first=v7.decode_selection(one,p);h=v7._digest
 common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'}
 first_input={**common,'messages_sha256':h(v7.messages(p)),'schema_sha256':h(v7.response_schema(p))}
 second_input={**common,'retained_unit_ids':first['unit_ids'],'messages_sha256':h(v7.review_messages(p,first)),'schema_sha256':h(v7.review_schema(p,first)),'input_tokens':100,'output_tokens':512,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first_input,'reviewed_claim_review_input':second_input,'input_budget':{**first_input,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192},'operation_plan':[op]}}
 assert v7.validate_brief(item,snips)==brief
 for field,value in [('retained_unit_ids',[]),('output_tokens',513),('input_tokens',True),('input_tokens',0),('input_tokens',8000),('context_tokens',9000),('model_visible_claim_ids',['C99'])]:
  altered=deepcopy(item);altered['provenance']['reviewed_claim_review_input'][field]=value
  with pytest.raises(ValueError):v7.validate_brief(altered,snips)


def test_first_budget_shape_and_reserve_are_validated():
 e=evidence();snips,_=reviewed.snippets(e);q='Who may apply?';op={'id':'q','request':q};one=enc({'unit_ids':['U2']});two=enc({'add_unit_ids':['U3'],'verdict':'supported'});brief,trace=v7.make_brief(q,op,e,snips,enc({'selection_raw':one,'review_raw':two}));p=v7.prepare(q,e,q);state=v7.decode_selection(one,p);h=v7._digest
 common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'};first={**common,'messages_sha256':h(v7.messages(p)),'schema_sha256':h(v7.response_schema(p))};second={**common,'retained_unit_ids':state['unit_ids'],'messages_sha256':h(v7.review_messages(p,state)),'schema_sha256':h(v7.review_schema(p,state)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};budget={**first,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first,'reviewed_claim_review_input':second,'input_budget':budget,'operation_plan':[op]}}
 for key,value in [('input_tokens',0),('input_tokens',7000),('output_tokens',512),('version','other'),('extra','x')]:
  altered=deepcopy(item);altered['provenance']['input_budget'][key]=value
  with pytest.raises(ValueError):v7.validate_brief(altered,snips)
