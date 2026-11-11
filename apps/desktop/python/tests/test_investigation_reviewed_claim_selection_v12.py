import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_reviewed_claim_selection_v12 as v
from potluck_local.services import investigation_reviewed_evidence as es
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel

def enc(x):return json.dumps(x,ensure_ascii=False)
def ev():
 t='## Rule\n- Approval is required.\n- Suspended members are excluded.'
 return [{'kind':'reviewed_claim','citation_id':'claim:a','content':t,'sources':[{'citation_id':'source:a:r1','content':t}]}]
def setup(q='What is the exact expiry date?'):
 p=v.prepare(q,ev());a=v.decode_selection(enc({'unit_ids':['U2']}),p);return p,a
def raw(ids=None,start='',end='',conflict=False):return enc({'review_unit_ids':['U2'] if ids is None else ids,'missing_start_token_id':start,'missing_end_token_id':end,'conflicting':conflict})

def test_schema_and_status_boundaries():
 p,a=setup();schema=v.review_schema(p,a)
 assert list(schema['properties'])==['review_unit_ids','missing_start_token_id','missing_end_token_id','conflicting']
 assert _structured_response_format(object.__new__(SplitModel),{'type':'json_object','schema':schema})
 assert v.decode_review(raw(),p,a)['status']=='supported'
 assert v.decode_review(raw(conflict=True),p,a)['status']=='conflicting'
 result=v.decode_review(raw(start='T4',end='T6'),p,a)
 assert result['status']=='unknown' and result['missing_request_quote']=='exact expiry date?' and result['offsets']==[12,30]
 assert v.decode_review(raw(ids=[],start='T5',end='T6'),p,a)['unit_ids']==[]
 # Shape-valid boundaries and blank missing fields cannot establish semantic correctness.
 assert v.decode_review(raw(start='T1',end='T1'),p,a)['missing_request_quote']=='What'
 assert 'Always retain known requested facts' in v.REVIEW_INSTRUCTION

def test_invalid_fields_and_boundary_combinations():
 p,a=setup()
 for data in [raw(start='T1'),raw(end='T1'),raw(start='T5',end='T4'),raw(start='T99',end='T99'),raw(start='T4',end='T6',conflict=True),raw(conflict=1),raw(ids=[]),enc({'review_unit_ids':['U2'],'missing_start_token_id':'','missing_end_token_id':'','conflicting':False,'extra':1}),'{"review_unit_ids":[],"missing_start_token_id":"","missing_start_token_id":"","missing_end_token_id":"","conflicting":false}']:
  with pytest.raises(ValueError):v.decode_review(data,p,a)
 q='A '+'x'*401;p,a=setup(q)
 with pytest.raises(ValueError):v.decode_review(raw(start='T2',end='T2'),p,a)
 p,a=setup(' '.join(['x']*129))
 with pytest.raises(ValueError):v.review_schema(p,a)

def test_full_replay_and_provenance_tamper():
 q='What is the exact expiry date?';e=ev();snips,_=es.snippets(e);op={'id':'q','request':q};one=enc({'unit_ids':['U2']});two=raw(start='T5',end='T6');bundle=enc({'selection_raw':one,'review_raw':two});brief,trace=v.make_brief(q,op,e,snips,bundle)
 assert brief['answers'][0]['status']=='unknown' and 'expiry date?' in brief['answers'][0]['text'] and 'scope' not in trace
 p=v.prepare(q,e,q);first=v.decode_selection(one,p);digest=v._digest;common={'model_visible_claim_ids':['C1'],'support_visibility':'Supporting source excerpts are not in the model prompt.'}
 inp={**common,'messages_sha256':digest(v.messages(p)),'schema_sha256':digest(v.response_schema(p))};review={**common,'proposal_unit_ids':first['requested_unit_ids'],'proposal_context_unit_ids':first['proposal_context_unit_ids'],'messages_sha256':digest(v.review_messages(p,first)),'schema_sha256':digest(v.review_schema(p,first)),'input_tokens':100,'output_tokens':512,'context_tokens':8192};budget={**inp,'version':'reviewed-claim-whole-v1','input_tokens':100,'output_tokens':2000,'context_tokens':8192}
 item={'issue':q,'evidence':e,'brief':brief,'provenance':{'reviewed_claim_selection':trace,'reviewed_claim_selection_input':inp,'reviewed_claim_review_input':review,'input_budget':budget,'operation_plan':[op]}}
 assert v.validate_brief(item,snips)==brief
 for field in ('boundary_policy','token_digest','offsets','dropped_proposal_unit_ids'):
  changed=deepcopy(item);changed['provenance']['reviewed_claim_selection']['review'][field]='tampered'
  with pytest.raises(ValueError):v.validate_brief(changed,snips)
 changed=deepcopy(item);changed['provenance']['reviewed_claim_scope_input']={}
 with pytest.raises(ValueError):v.validate_brief(changed,snips)
 with pytest.raises(ValueError):v.make_brief(q,op,e,snips,enc({'selection_raw':one,'review_raw':two,'scope_raw':'{}'}))

def test_proposal_expansion_final_closure_and_overflow():
 text='## One\n- A helper `convert(x)` acts.\n'+'\n'.join('- Consumer uses `convert` for item '+str(i)+'.' for i in range(17));other='## Two\n- Approval is required.'
 e=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]},{'kind':'reviewed_claim','citation_id':'claim:b','content':other,'sources':[{'citation_id':'source:b:r1','content':other}]}]
 p=v.prepare('What is required?',e);edge=v._graph(p)[0];first=v.decode_selection(enc({'unit_ids':[edge['from']]}),p);assert len(first['proposal_context_unit_ids'])>16
 target=next(u['id'] for u in p['units'] if u['claim_id']=='C2' and 'Approval' in u['text']);final=v.decode_review(raw(ids=[target]),p,first)
 assert target in final['unit_ids'] and edge['from'] in final['dropped_proposal_unit_ids'] and edge['to'] in final['dropped_dependency_only_unit_ids']
 with pytest.raises(ValueError,match='context limit'):v.decode_review(raw(ids=[edge['from']]),p,first)
 for ids in ([target,target],['U999']):
  with pytest.raises(ValueError):v.decode_selection(enc({'unit_ids':ids}),p)

def test_unicode_whitespace_exact_and_mixed_known_context():
 q='Known rule  café ☕ expiry?';p,a=setup(q)
 result=v.decode_review(raw(ids=['U2'],start='T3',end='T5'),p,a)
 assert result['missing_request_quote']=='café ☕ expiry?' and 'U2' in result['unit_ids']
 assert p['request'][slice(*result['offsets'])]==result['missing_request_quote']

def test_numbered_prerequisite_retained_and_source_tamper_rejected():
 text='## Procedure\n1. Check eligibility.\n2. Grant permission.'
 e=[{'kind':'reviewed_claim','citation_id':'claim:a','content':text,'sources':[{'citation_id':'source:a:r1','content':text}]}];p=v.prepare('How is permission granted?',e)
 step=next(u for u in p['units'] if 'Grant permission' in u['text']);first=v.decode_selection(enc({'unit_ids':[step['id']]}),p);final=v.decode_review(raw(ids=[step['id']]),p,first)
 assert set(step['required_unit_ids'])<=set(final['unit_ids'])
 bad=deepcopy(p);bad['units'][0]['text']='changed'
 with pytest.raises(ValueError):v.decode_review(raw(ids=[step['id']]),bad,first)

def test_fully_absent_unknown_renders_without_invented_evidence():
 q='What is the exact expiry date?';e=ev();snips,_=es.snippets(e);one=enc({'unit_ids':[]});two=raw(ids=[],start='T5',end='T6')
 brief,trace=v.make_brief(q,{'id':'q','request':q},e,snips,enc({'selection_raw':one,'review_raw':two}))
 assert brief['answers'][0]['status']=='unknown' and 'expiry date?' in brief['answers'][0]['text']
 assert trace['review']['unit_ids']==[]
