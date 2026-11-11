import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v11 as v11
from potluck_local.services import investigation_reviewed_evidence as es
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel

def evidence():
 text='## Policy\n- Approval required.\n- Suspended members excluded.'
 return [{'kind':'reviewed_claim','citation_id':'claim:one','content':text,'sources':[{'citation_id':'source:one:rev','content':text}]}]
def enc(x):return json.dumps(x,ensure_ascii=False)
def test_known_review_has_no_scope():
 p=v11.prepare('Who may apply?',evidence());one=v11.decode_selection(enc({'unit_ids':['U2']}),p)
 for status in ('supported','conflicting'):
  two=v11.decode_review(enc({'review_unit_ids':['U3'],'status':status}),p,one)
  assert two['status']==status
  with pytest.raises(ValueError):v11.scope_schema(p,one,two)
  with pytest.raises(ValueError):v11.make_brief('Who may apply?',{'id':'q','request':'Who may apply?'},evidence(),es.snippets(evidence())[0],enc({'selection_raw':one['raw'],'review_raw':two['raw'],'scope_raw':'{}'}))
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v11.review_schema(p,one)})

def test_unknown_exact_quote_and_state_revalidation():
 p=v11.prepare('Broader secret date',evidence(),'What is the date?');one=v11.decode_selection(enc({'unit_ids':['U2']}),p);two=v11.decode_review(enc({'review_unit_ids':[],'status':'unknown'}),p,one)
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':v11.scope_schema(p,one,two)})
 assert v11.decode_scope(enc({'request_token_ids':['T4']}),p,one,two)['missing_request_quote']=='date?'
 for q in ([],['T2','T4'],['T4','T3'],['T4','T4'],['T9']):
  with pytest.raises(ValueError):v11.decode_scope(enc({'request_token_ids':q}),p,one,two)
 for raw in ('{"request_token_ids":["T4"],"request_token_ids":["T4"]}',enc({'request_token_ids':['T4'],'status':'supported'})):
  with pytest.raises(ValueError):v11.decode_scope(raw,p,one,two)
 forged=deepcopy(two);forged['unit_ids']=['U2']
 with pytest.raises(ValueError):v11.scope_messages(p,one,forged)

def test_unknown_bundle_and_strict_replay():
 e=evidence();snips,_=es.snippets(e);q='What is the date?';op={'id':'q','request':q};a=enc({'unit_ids':['U2']});b=enc({'review_unit_ids':['U3'],'status':'unknown'});c=enc({'request_token_ids':['T4']});bundle=enc({'selection_raw':a,'review_raw':b,'scope_raw':c});brief,trace=v11.make_brief(q,op,e,snips,bundle)
 assert brief['answers'][0]['status']=='unknown' and 'date' in brief['answers'][0]['text'] and trace['renderer_version']=='reviewed-claim-selection-v6'
 p=v11.prepare(q,e,q);one=v11.decode_selection(a,p);two=v11.decode_review(b,p,one);h=v11._digest;common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'}
 first={**common,'messages_sha256':h(v11.messages(p)),'schema_sha256':h(v11.response_schema(p))};review={**common,'proposal_unit_ids':one['requested_unit_ids'],'proposal_context_unit_ids':one['proposal_context_unit_ids'],'messages_sha256':h(v11.review_messages(p,one)),'schema_sha256':h(v11.review_schema(p,one)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};scope={**common,'reviewed_unit_ids':two['unit_ids'],'status':'unknown','messages_sha256':h(v11.scope_messages(p,one,two)),'schema_sha256':h(v11.scope_schema(p,one,two)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};budget={**first,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':first,'reviewed_claim_review_input':review,'reviewed_claim_scope_input':scope,'input_budget':budget,'operation_plan':[op]}}
 assert v11.validate_brief(item,snips)==brief
 bad=deepcopy(item);bad['provenance']['reviewed_claim_selection']['context_edges']=[{'from':'U1','to':'U2'}]
 with pytest.raises(ValueError):v11.validate_brief(bad,snips)
 bad=deepcopy(item);bad['provenance']['reviewed_claim_selection']['review']['dropped_proposal_unit_ids']=['U999']
 with pytest.raises(ValueError):v11.validate_brief(bad,snips)
 for key,value in [('reviewed_unit_ids',[]),('status','supported'),('output_tokens',2000),('input_tokens',0),('context_tokens',9000)]:
  bad=deepcopy(item);bad['provenance']['reviewed_claim_scope_input'][key]=value
  with pytest.raises(ValueError):v11.validate_brief(bad,snips)
 with pytest.raises(ValueError):v11.make_brief(q,op,e,snips,enc({'selection_raw':a,'review_raw':b}))


def test_same_claim_helper_use_graph_and_cross_claim_guard():
 text='## Helpers\n- A helper `convert(x)` performs conversion.\n- A consumer uses `convert` for display.\n- Another mentions `convert` only.'
 other='## Elsewhere\n- A consumer uses `convert` for another claim.'
 ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]},{'kind':'reviewed_claim','citation_id':'claim:b','content':other,'sources':[{'citation_id':'source:b:r1','content':other}]}]
 p=v11.prepare('What uses conversion?',ev);edges=v11._graph(p)
 assert len(edges)==1 and edges[0]['claim_id']=='C1'
 state=v11.decode_selection(enc({'unit_ids':[edges[0]['from']]}),p)
 assert edges[0]['to'] in state['proposal_context_unit_ids']
 assert all(x['claim_id']=='C1' for x in edges)

def test_scope_exact_whitespace_and_unicode_and_limit():
 q='Which café  ☕ expiry date?'
 p=v11.prepare(q,evidence());a=v11.decode_selection(enc({'unit_ids':['U2']}),p);b=v11.decode_review(enc({'review_unit_ids':[],'status':'unknown'}),p,a)
 result=v11.decode_scope(enc({'request_token_ids':['T2','T3','T4']}),p,a,b)
 assert result['missing_request_quote']=='café  ☕ expiry'
 assert result['tokenizer']=='request-token-spans-v1'
 p=v11.prepare(' '.join(['x']*129),evidence());a=v11.decode_selection(enc({'unit_ids':['U2']}),p);b=v11.decode_review(enc({'review_unit_ids':[],'status':'unknown'}),p,a)
 with pytest.raises(ValueError,match='token limit'):v11.scope_schema(p,a,b)

def test_graph_rejects_ambiguous_and_fenced_pseudodefinitions():
 text='## A\n- A helper `run(x)` is introduced.\n- Another helper `run(y)` is introduced.\n- A consumer uses `run`.\n\n```\nhelper `fake(x)`\n```\n- A consumer uses `fake`.'
 ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]}]
 assert v11._graph(v11.prepare('Which helper?',ev))==[]

def test_scope_rejects_long_slice_duplicate_keys_and_tampered_state():
 q='A '+'x'*401
 p=v11.prepare(q,evidence());one=v11.decode_selection(enc({'unit_ids':['U2']}),p);two=v11.decode_review(enc({'review_unit_ids':[],'status':'unknown'}),p,one)
 for raw in (enc({'request_token_ids':['T2']}),'{"request_token_ids":["T1"],"request_token_ids":["T1"]}'):
  with pytest.raises(ValueError):v11.decode_scope(raw,p,one,two)
 bad=deepcopy(p);bad['request']='changed'
 with pytest.raises(ValueError):v11.decode_scope(enc({'request_token_ids':['T1']}),bad,one,two)


def test_identifier_case_and_nonadjacent_definition_are_not_links():
 text='## One\n- A helper `Convert(x)` acts.\n- A consumer uses `convert`.\n- A helper forwards to other calls `fake(x)`.\n- A consumer uses `fake`.'
 ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]}]
 assert v11._graph(v11.prepare('What happens?',ev))==[]

def test_helper_closure_fails_above_unit_cap():
 text='## One\n- A helper `convert(x)` acts.\n'+'\n'.join('- Consumer uses `convert` for item '+str(i)+'.' for i in range(17))
 ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]}]
 p=v11.prepare('What uses helper?',ev);edges=v11._graph(p);assert len(edges)==17
 first=v11.decode_selection(enc({'unit_ids':[edges[0]['from']]}),p)
 assert len(first['proposal_context_unit_ids'])>16
 with pytest.raises(ValueError,match='context limit'):v11.decode_review(enc({'review_unit_ids':[edges[0]['from']],'status':'supported'}),p,first)


def test_large_proposal_is_replaceable_and_records_dropped_context():
 text='## One\n- A helper `convert(x)` acts.\n'+'\n'.join('- Consumer uses `convert` for item '+str(i)+'.' for i in range(17))
 other='## Two\n- Approval is required.'
 ev=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]},{'kind':'reviewed_claim','citation_id':'claim:b','content':other,'sources':[{'citation_id':'source:b:r1','content':other}]}]
 p=v11.prepare('What is required?',ev);edge=v11._graph(p)[0];first=v11.decode_selection(enc({'unit_ids':[edge['from']]}),p)
 assert len(first['proposal_context_unit_ids'])>16
 target=next(u['id'] for u in p['units'] if u['claim_id']=='C2' and 'Approval' in u['text'])
 final=v11.decode_review(enc({'review_unit_ids':[target],'status':'supported'}),p,first)
 assert target in final['unit_ids'] and len(final['unit_ids'])<=16 and edge['from'] not in final['unit_ids']
 assert final['dropped_proposal_unit_ids']==[edge['from']] and edge['to'] in final['dropped_dependency_only_unit_ids']
 with pytest.raises(ValueError):v11.decode_selection(enc({'unit_ids':[target,target]}),p)
 with pytest.raises(ValueError):v11.decode_selection(enc({'unit_ids':['U999']}),p)
