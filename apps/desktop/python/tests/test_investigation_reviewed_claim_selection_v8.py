import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v8 as v8
from potluck_local.services import investigation_reviewed_evidence as evidence_service
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel

def evidence():
 text='## Policy\n- Approval is required.\n- Suspended members are excluded.'
 return [{'kind':'reviewed_claim','citation_id':'claim:one','content':text,'sources':[{'citation_id':'source:one:rev','content':text}]}]
def raw(value):return json.dumps(value)
def test_schema_closure_and_independent_review():
 p=v8.prepare('Who may apply?',evidence());one=v8.decode_selection(raw({'unit_ids':['U2']}),p)
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v8.review_schema(p,one)})
 two=v8.decode_review(raw({'review_unit_ids':['U3'],'verdict':{'supported':None}}),p,one)
 assert set(one['unit_ids'])<=set(two['unit_ids']) and 'U3' in two['unit_ids']
 assert 'all requested facts' in v8.review_messages(p,one)[0]['content']
 assert v8.decode_review(raw({'review_unit_ids':['U2'],'verdict':{'conflicting':None}}),p,one)['status']=='conflicting'

def test_strict_one_key_and_request_binding():
 p=v8.prepare('Broader secret deadline','What is the date?' if False else evidence(),'What is the date?');one=v8.decode_selection(raw({'unit_ids':['U2']}),p)
 invalid=[{}, {'supported':None,'conflicting':None},{'supported':'yes'},{'unknown':''},{'unknown':' '},{'unknown':'secret deadline'},{'unknown':'x'*401},{'other':None},'supported']
 for verdict in invalid:
  with pytest.raises(ValueError):v8.decode_review(raw({'review_unit_ids':[],'verdict':verdict}),p,one)
 assert v8.decode_review(raw({'review_unit_ids':[],'verdict':{'unknown':'date'}}),p,one)['missing_request_quote']=='date'
 for value in ['{"review_unit_ids":[],"verdict":{"supported":null,"supported":null}}', '{"review_unit_ids":[],"verdict":{"supported":null},"extra":1}']:
  with pytest.raises(ValueError):v8.decode_review(value,p,one)

def test_bundle_and_budget_tamper():
 e=evidence();snips,_=evidence_service.snippets(e);q='Who may apply?';op={'id':'q','request':q};first=raw({'unit_ids':['U2']});second=raw({'review_unit_ids':['U3'],'verdict':{'supported':None}});bundle=raw({'selection_raw':first,'review_raw':second});brief,trace=v8.make_brief(q,op,e,snips,bundle)
 assert trace['version']==v8.VERSION and trace['renderer_version']=='reviewed-claim-selection-v6'
 assert 'Approval is required' in brief['answers'][0]['text'] and 'Suspended members' in brief['answers'][0]['text']
 p=v8.prepare(q,e,q);one=v8.decode_selection(first,p);h=v8._digest;common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'};first_input={**common,'messages_sha256':h(v8.messages(p)),'schema_sha256':h(v8.response_schema(p))};review_input={**common,'retained_unit_ids':one['unit_ids'],'messages_sha256':h(v8.review_messages(p,one)),'schema_sha256':h(v8.review_schema(p,one)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};budget={**first_input,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first_input,'reviewed_claim_review_input':review_input,'input_budget':budget,'operation_plan':[op]}}
 assert v8.validate_brief(item,snips)==brief
 bad=deepcopy(item);bad['provenance']['reviewed_claim_selection']['selection']=[]
 with pytest.raises(ValueError):v8.validate_brief(bad,snips)
 for field,value in [('retained_unit_ids',[]),('input_tokens',0),('output_tokens',513),('context_tokens',9000)]:
  bad=deepcopy(item);bad['provenance']['reviewed_claim_review_input'][field]=value
  with pytest.raises(ValueError):v8.validate_brief(bad,snips)
