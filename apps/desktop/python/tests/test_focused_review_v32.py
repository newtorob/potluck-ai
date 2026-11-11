"""Focused requests, actual two-stage service, and immutable wire compatibility."""
import asyncio,json
from copy import deepcopy
import pytest
from test_investigation_single_review_v28 import prepared as old_prepared,wire
from potluck_local.services import investigation_part_selection_v28 as baseline
from potluck_local.services import investigation_part_selection_v32 as v
from potluck_local.services import investigation_fixed_part_answers_v32 as a
from potluck_local.services import investigation_request_parts as parts

def setup(question='Explain the approval rule.',first_part='Which rule?',second_part='What is additionally known?'):
 _,ev,plans,_,_=old_prepared();plan=parts.prepare(question,[first_part,second_part],origin="fixture_supplied",request=question)
 p=v.prepare(question,ev,plan,unit_plans=plans);return question,ev,plans,plan,p,v.authored_inventory(p)

def test_ordinary_wire_is_exactly_v28_but_application_binding_is_distinct():
 q,e,u,plan,p,first=setup();old=baseline.prepare(q,e,plan,unit_plans=u);prior=baseline.authored_inventory(old);raw=wire('supported',['C1:rule'])
 assert p['version']==v.VERSION and p['input_sha256']!=old['input_sha256']
 assert v.MODEL_INPUT_CONTRACT==baseline.VERSION
 assert v.review_messages(p,first)==baseline.review_messages(old,prior)
 assert v.review_schema(p,first)==baseline.review_schema(old,prior)
 assert v.review_focus(p,first,raw)==[]
 assert v.necessity_messages(p,first,raw)==baseline.necessity_messages(old,prior,raw)
 assert a.review_receipt(p,first)['model_input_contract']==baseline.VERSION
 assert a.necessity_receipt(p,first,raw)['review_focus']==[]

@pytest.mark.parametrize('question,expected',[
 ('Which exact time zone is specified?',True),('How many days are allowed?',True),
 ('What is the deadline for finishing repairs?',True),('Which timestamp is recorded?',True),
 ('How long do records last?',True),('What expiry is stated?',True),
 ('Which events count as delivery?',False),('In the September 16 design, how is intent stored?',False),
 ('Are the two default sections consistent?',False),('Explain the required actions before recovery.',False)])
def test_request_focus_uses_property_intent_not_bare_numbers_or_count_verb(question,expected):
 *_,p,first=setup(question);focus=v.review_focus(p,first,wire('supported',['C1:rule']))
 assert ('precise-property-and-event' in focus)==expected
 assert len(json.loads(v.necessity_messages(p,first,wire('supported',['C1:rule']))[1]['content'])['claims'])==len(p['claims'])

def test_unknown_scope_remains_strict_but_proposal_can_be_repaired():
 *_,p,first=setup();empty=wire('unknown',[])
 assert v.decode_proposal(empty,p,first)['status']=='unknown'
 assert 'unknown-visible-scope' in v.review_focus(p,first,empty)
 with pytest.raises(ValueError,match='scope anchor'):v.decode_review(empty,p,first)
 assert 'Approval is required.' in v.decode_review(wire('unknown',['C1:rule']),p,first)['text']

@pytest.mark.parametrize('text,expected',[
 ('See the residual-risk section.',True),('As noted above, a limitation applies.',True),
 ('Refer to the attestation TODO.',True),('A control above the preview.',False),
 ('The webhook topic reference describes events.',False)])
def test_reference_focus_checks_visible_basis_not_background_only(text,expected):
 _,ev,_,_,_=old_prepared();ev=deepcopy(ev);ev[0]['content']=text
 q='Explain the selected rule.';plan=parts.prepare(q,['Which rule?'],origin="fixture_supplied",request=q)
 p=v.prepare(q,ev,plan);first=v.authored_inventory(p);uid=p['catalog']['units'][0]['id']
 raw=json.dumps({'parts':{'P1':{'evidence':[uid],'background':[],'verdict':'supported'}},'safeguards':[]})
 assert ('explicit-source-reference' in v.review_focus(p,first,raw))==expected
 bg=json.dumps({'parts':{'P1':{'evidence':[],'background':[uid],'verdict':'unknown'}},'safeguards':[]})
 assert 'explicit-source-reference' not in v.review_focus(p,first,bg)


def test_dependency_exception_and_incompatible_alternatives_survive():
 *_,p,first=setup();result=v.decode_review(wire('conflicting',['C1:rule','C1:other']),p,first)
 assert 'Emergency work is exempt.' in result['text']
 assert 'No approval is required.' in result['text']
 assert 'unresolved incompatible alternatives' in result['text']


def test_actual_service_binds_model_contract_and_both_actual_replies(tmp_path,monkeypatch):
 import test_investigation_fixed_part_answers_service as shared
 from potluck_local.services import investigations as inv
 class Runtime(shared.FixedRuntime):
  async def chat(self,messages,**kw):
   self.calls.append(deepcopy((messages,kw)))
   raw=wire('supported',['C1:rule'])
   return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}]}
 async def run():
  monkeypatch.setattr(shared,'MODE','fixed-part-selection-v32');monkeypatch.setattr(shared.routes,'_error',lambda e:e)
  _,scope,*_=await shared.setup(tmp_path,monkeypatch);runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
  item=await shared.create(scope);calls=runtime.calls
  assert len(calls)==2 and all(kw['max_tokens']==256 for _,kw in calls)
  assert calls[0][1]['response_format']['schema']==calls[1][1]['response_format']['schema']
  assert all(json.loads(messages[1]['content'])['contract']=='fixed-part-selection-v28' for messages,_ in calls)
  budget=item['provenance']['input_budget'];assert budget['version']=='fixed-part-focused-review-v32'
  assert all(row['model_input_contract']=='fixed-part-selection-v28' for row in budget['stages'])
  assert budget['stages'][0]['messages_sha256']!=budget['stages'][1]['messages_sha256']
  trace=item['provenance']['reviewed_claim_selection'];assert set(json.loads(trace['raw']))=={'selection_raw','review_raw'}
  assert trace['version']=='fixed-part-selection-v32'
  assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
  await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
 asyncio.run(run())
