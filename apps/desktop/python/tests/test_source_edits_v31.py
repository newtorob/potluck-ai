"""Native edits are distinct from application-derived source selection and semantic truth."""
import asyncio,json
from copy import deepcopy
import pytest
from test_investigation_single_review_v28 import prepared as old_prepared,wire
from potluck_local.services import investigation_part_selection_v31 as v
from potluck_local.services import investigation_fixed_part_answers_v31 as a
from potluck_local.services import investigation_reviewed_evidence

def setup():
 q,ev,plans,part,_=old_prepared();p=v.prepare(q,ev,part,unit_plans=plans)
 return q,ev,plans,part,p,v.authored_inventory(p)

def edits(drop=(),add=(),verdict='supported'):
 return {'parts':{'P1':{'drop':list(drop),'add':list(add),'verdict':verdict},'P2':{'drop':[],'add':[],'verdict':'unknown'}},'safeguards':{'drop':[],'add':[]}}

def test_drop_root_does_not_drop_required_dependency():
 *_,p,first=setup();proposal=wire('supported',['C1:rule','C1:exception'])
 result=v.decode_edits(json.dumps(edits(['C1:exception'])),p,first,proposal)
 assert 'Emergency work is exempt.' in result['text']
 assert 'C1:exception' in result['source_edit_application']['dropped_roots_still_visible']['P1']
 assert result['parts'][0]['evidence_unit_ids']==['C1:rule']

def test_drop_unneeded_root_and_add_alternative_keep_native_and_derived_distinct():
 q,ev,plans,part,p,first=setup();proposal=wire('supported',['C1:rule','C1:other'])
 native=json.dumps(edits(['C1:other']));result=v.decode_edits(native,p,first,proposal)
 assert 'No approval is required.' not in result['text']
 assert result['review_raw']==native and result['applied_selection_raw']!=native
 assert result['source_edit_application']['origin']=='application-derived-selection-not-native-reply'
 assert v.validate_edit_result(q,ev,part,proposal,native,result,unit_plans=plans)==result
 alternative=v.decode_edits(json.dumps(edits([],['C1:other'],'conflicting')),p,first,wire('supported',['C1:rule']))
 assert alternative['status']=='conflicting' and 'No approval is required.' in alternative['text']

@pytest.mark.parametrize('kind',['foreign_drop','overlap','bad_id','missing_part','extra_key','bad_verdict','duplicate_id'])
def test_invalid_edits_fail_closed(kind):
 *_,p,first=setup();data=edits();row=data['parts']['P1']
 if kind=='foreign_drop':row['drop']=['C1:other']
 elif kind=='overlap':row.update(drop=['C1:rule'],add=['C1:rule'])
 elif kind=='bad_id':row['add']=['C999:missing']
 elif kind=='missing_part':del data['parts']['P2']
 elif kind=='extra_key':row['approval']=True
 elif kind=='bad_verdict':row['verdict']=True
 else:row['add']=['C1:other','C1:other']
 with pytest.raises(ValueError):v.decode_edits(json.dumps(data),p,first,wire('supported',['C1:rule']))

def test_edits_cannot_remove_last_unknown_anchor_or_reuse_plain_selection_reply():
 *_,p,first=setup();proposal=wire('unknown',['C1:rule'])
 with pytest.raises(ValueError,match='scope anchor'):v.decode_edits(json.dumps(edits(['C1:rule'],verdict='unknown')),p,first,proposal)
 with pytest.raises(ValueError,match='source-edit'):v.decode_edits(wire('unknown',['C1:rule']),p,first,proposal)
 repaired=v.decode_edits(json.dumps(edits([],['C1:rule'],'unknown')),p,first,wire('unknown',[]))
 assert repaired['status']=='unknown' and 'Approval is required.' in repaired['text']

def test_dropping_one_part_root_preserves_other_part_and_guard_scope():
 *_,p,first=setup();data=json.loads(wire('supported',['C1:rule']));data['parts']['P2']['evidence']=['C1:rule'];proposal=json.dumps(data)
 delta=edits(['C1:rule'],verdict='unknown');delta['safeguards']['add']=['C1:other']
 result=v.decode_edits(json.dumps(delta),p,first,proposal)
 assert result['parts'][0]['visible_unit_ids']==[] and 'C1:rule' in result['parts'][1]['visible_unit_ids']
 assert result['request_context']['requested_unit_ids']==['C1:other']
 assert 'C1:rule' in result['source_edit_application']['dropped_roots_still_visible']['P1']

def test_real_two_stage_service_uses_distinct_schema_receipts_and_reopens(tmp_path,monkeypatch):
 import test_investigation_fixed_part_answers_service as shared
 from potluck_local.services import investigations as inv
 class Runtime(shared.FixedRuntime):
  async def chat(self,messages,**kw):
   self.calls.append(deepcopy((messages,kw)))
   raw=wire('supported',['C1:rule','C1:other']) if len(self.calls)==1 else json.dumps(edits(['C1:other']))
   return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}]}
 async def run():
  monkeypatch.setattr(shared,'MODE','fixed-part-selection-v31');monkeypatch.setattr(shared.routes,'_error',lambda e:e)
  _,scope,*_=await shared.setup(tmp_path,monkeypatch);runtime=Runtime();monkeypatch.setattr(inv,'get_runtime',lambda:runtime)
  item=await shared.create(scope);calls=runtime.calls
  assert len(calls)==2 and all(kw['max_tokens']==256 for _,kw in calls)
  assert list(calls[0][1]['response_format']['schema']['properties']['parts']['properties']['P1']['properties'])==['evidence','background','verdict']
  assert list(calls[1][1]['response_format']['schema']['properties']['parts']['properties']['P1']['properties'])==['drop','add','verdict']
  budget=item['provenance']['input_budget'];assert budget['version']=='fixed-part-edit-review-v31'
  assert budget['stages'][0]['schema_sha256']!=budget['stages'][1]['schema_sha256']
  assert 'Unrelated retention is 30 days.' not in item['brief']['answers'][0]['text']
  trace=item['provenance']['reviewed_claim_selection'];raw=json.loads(trace['raw']);assert raw['review_raw']==trace['review']['review_raw']
  assert json.loads(raw['review_raw'])['parts']['P1']['drop']==['C1:other']
  assert (await inv.get_investigation(scope,item['id']))['brief']==item['brief']
  await shared.routes.export(item['id'],scope.workspace,list(scope.collection_ids))
 asyncio.run(run())
