import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v9 as v9
from potluck_local.services import investigation_reviewed_evidence as es
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel

def evidence():
 text='## Policy\n- Approval required.\n- Suspended members excluded.'
 return [{'kind':'reviewed_claim','citation_id':'claim:one','content':text,'sources':[{'citation_id':'source:one:rev','content':text}]}]
def enc(x):return json.dumps(x,ensure_ascii=False)
def test_known_review_has_no_scope():
 p=v9.prepare('Who may apply?',evidence());one=v9.decode_selection(enc({'unit_ids':['U2']}),p)
 for status in ('supported','conflicting'):
  two=v9.decode_review(enc({'review_unit_ids':['U3'],'status':status}),p,one)
  assert set(one['unit_ids'])<=set(two['unit_ids']) and two['status']==status
  with pytest.raises(ValueError):v9.scope_schema(p,one,two)
  with pytest.raises(ValueError):v9.make_brief('Who may apply?',{'id':'q','request':'Who may apply?'},evidence(),es.snippets(evidence())[0],enc({'selection_raw':one['raw'],'review_raw':two['raw'],'scope_raw':'{}'}))
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v9.review_schema(p,one)})

def test_unknown_exact_quote_and_state_revalidation():
 p=v9.prepare('Broader secret date',evidence(),'What is the date?');one=v9.decode_selection(enc({'unit_ids':['U2']}),p);two=v9.decode_review(enc({'review_unit_ids':[],'status':'unknown'}),p,one)
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v9.scope_schema(p,one,two)})
 assert v9.decode_scope(enc({'missing_request_quote':'date'}),p,one,two)['missing_request_quote']=='date'
 for q in ('',' ','secret date','x'*401):
  with pytest.raises(ValueError):v9.decode_scope(enc({'missing_request_quote':q}),p,one,two)
 for raw in ('{"missing_request_quote":"date","missing_request_quote":"date"}',enc({'missing_request_quote':'date','status':'supported'})):
  with pytest.raises(ValueError):v9.decode_scope(raw,p,one,two)
 forged=deepcopy(two);forged['unit_ids']=[]
 with pytest.raises(ValueError):v9.scope_messages(p,one,forged)

def test_unknown_bundle_and_strict_replay():
 e=evidence();snips,_=es.snippets(e);q='What is the date?';op={'id':'q','request':q};a=enc({'unit_ids':['U2']});b=enc({'review_unit_ids':['U3'],'status':'unknown'});c=enc({'missing_request_quote':'date'});bundle=enc({'selection_raw':a,'review_raw':b,'scope_raw':c});brief,trace=v9.make_brief(q,op,e,snips,bundle)
 assert brief['answers'][0]['status']=='unknown' and 'date' in brief['answers'][0]['text'] and trace['renderer_version']=='reviewed-claim-selection-v6'
 p=v9.prepare(q,e,q);one=v9.decode_selection(a,p);two=v9.decode_review(b,p,one);h=v9._digest;common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'}
 first={**common,'messages_sha256':h(v9.messages(p)),'schema_sha256':h(v9.response_schema(p))};review={**common,'retained_unit_ids':one['unit_ids'],'messages_sha256':h(v9.review_messages(p,one)),'schema_sha256':h(v9.review_schema(p,one)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};scope={**common,'reviewed_unit_ids':two['unit_ids'],'status':'unknown','messages_sha256':h(v9.scope_messages(p,one,two)),'schema_sha256':h(v9.scope_schema(p,one,two)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};budget={**first,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first,'reviewed_claim_review_input':review,'reviewed_claim_scope_input':scope,'input_budget':budget,'operation_plan':[op]}}
 assert v9.validate_brief(item,snips)==brief
 for key,value in [('reviewed_unit_ids',[]),('status','supported'),('output_tokens',2000),('input_tokens',0),('context_tokens',9000)]:
  bad=deepcopy(item);bad['provenance']['reviewed_claim_scope_input'][key]=value
  with pytest.raises(ValueError):v9.validate_brief(bad,snips)
 with pytest.raises(ValueError):v9.make_brief(q,op,e,snips,enc({'selection_raw':a,'review_raw':b}))
