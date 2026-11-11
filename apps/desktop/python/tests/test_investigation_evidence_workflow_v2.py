import asyncio
from copy import deepcopy
import json
import pytest
from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_coverage as v1
from potluck_local.services import investigation_evidence_coverage_v2 as v2
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection
from test_investigation_evidence_workflow import prepared, Runtime
from test_investigation_verification import setup


class VersionRuntime(Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); normalized = deepcopy(messages)
        if payload['version'] == v2.VERSION:
            replacement = dict(payload, version=v1.VERSION)
            normalized[1]['content'] = json.dumps(replacement)
        result = await super().chat(normalized, **kwargs)
        self.calls[-1] = (payload, kwargs)
        return result


def candidate(technical=False):
    return workflow.prepare(prepared(technical)['original'], version=workflow.V2_VERSION)


@pytest.mark.asyncio
@pytest.mark.parametrize('technical', [False, True])
async def test_opt_in_version_runs_complete_workflow_and_preserves_default(technical):
    p = prepared(technical); q = candidate(technical)
    oldruntime, newruntime = VersionRuntime(), VersionRuntime()
    a, b = await asyncio.gather(workflow.generate(p, oldruntime, model='model', model_sha='sha'),
                                workflow.generate(q, newruntime, model='model', model_sha='sha'))
    assert a['status'] == b['status'] == 'proposal'
    assert a['version'] == a['result']['version'] == workflow.VERSION
    assert b['version'] == b['result']['version'] == workflow.V2_VERSION
    assert b['result']['component_versions']['context'] == v2.VERSION
    assert a['generation_calls'] == b['generation_calls'] == (3 if technical else 1)
    assert oldruntime.calls[0][0]['version'] == v1.VERSION
    assert newruntime.calls[0][0]['version'] == v2.VERSION
    schema = newruntime.calls[0][1]['response_format']['schema']
    assert schema['properties']['comparisons']['maxItems'] == (2 if technical else 0)
    assert workflow.prepare(p['original']) == p


def test_selected_focus_controls_gate_not_full_question():
    env = prepared()['original']; env['single_focus'] = False
    env['group'].update(request='Which version and who approves the policy?', focus='Policy approval')
    p = workflow.prepare(env, version=workflow.V2_VERSION)
    assert v2.response_schema(p)['properties']['comparisons']['maxItems'] == 0
    payload = json.loads(v2.messages(p)[1]['content'])
    assert payload['original_question'] == env['group']['request']
    assert payload['question'] == 'Policy approval'


def test_wrong_context_version_cannot_be_composed():
    p = prepared(); q = candidate()
    raw = json.dumps({'verdict':'source_rule','units':{u['id']:('answer' if i==0 else 'background')
       for i,u in enumerate(p['units'])},'dependencies':[],'comparisons':[]})
    old = v1.decode(raw,p); new = v2.decode(raw,q)
    with pytest.raises(ValueError): workflow.compose(p,new)
    with pytest.raises(ValueError): workflow.compose(q,old)


@pytest.mark.asyncio
async def test_invalid_version_before_inference_and_cancel():
    p = candidate(); p['version'] = 'unknown'; runtime = VersionRuntime()
    with pytest.raises(ValueError): await workflow.generate(p,runtime,model='model',model_sha='sha')
    with pytest.raises(ValueError): workflow.prepare(prepared()['original'],version='unknown')
    assert runtime.calls == []
    runtime = VersionRuntime('cancel'); task = asyncio.create_task(workflow.generate(candidate(),runtime,model='model',model_sha='sha'))
    await runtime.entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


@pytest.mark.asyncio
@pytest.mark.parametrize('race',['none','review','rescan'])
async def test_scoped_v2_proposal_is_detached_and_checks_state(setup,race):
    runtime,scope,original,path=await setup()
    with pytest.raises(ValueError): await workflow.propose(scope,original['id'],'G1',version='unknown')
    with pytest.raises(LookupError): await workflow.propose(EvidenceScope('other',scope.collection_ids),original['id'],'G1',version=workflow.V2_VERSION)
    assert runtime.calls == []
    async def chat(messages,**kwargs):
        payload=json.loads(messages[1]['content']);runtime.calls.append(payload)
        assert payload['version']==v2.VERSION
        assert kwargs['response_format']['schema']['properties']['comparisons']['maxItems']==0
        if race=='review': await inv.review_investigation(scope,original['id'],'accept','owner','Reviewed.')
        if race=='rescan':
            path.write_text('# Changed\n\nDifferent source.\n');await scan_collection('w',scope.collection_ids[0])
        raw={'verdict':'source_rule','units':{u['id']:('answer' if i==0 else 'background') for i,u in enumerate(payload['units'])},'dependencies':[],'comparisons':[]}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(raw)}}]}
    runtime.chat=chat
    if race=='none':
        r=await workflow.propose(scope,original['id'],'G1',version=workflow.V2_VERSION)
        assert r['attempt']['result']['version']==workflow.V2_VERSION
        assert await inv.get_investigation(scope,original['id'])==original
    else:
        with pytest.raises((ValueError,LookupError)): await workflow.propose(scope,original['id'],'G1',version=workflow.V2_VERSION)
    assert len(runtime.calls)==1
