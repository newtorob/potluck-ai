import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_evidence_workflow as workflow
from potluck_local.services import investigation_evidence_coverage_v3 as context
from potluck_local.services import investigation_evidence_comparisons as source
from potluck_local.services import investigation_value_inventory_v2 as prior
from potluck_local.services import investigation_value_inventory_v3 as inventory
from potluck_local.services.runtime import _structured_response_format
from potluck_local.services.split_native import SplitModel
from test_investigation_evidence_workflow_v4 import prepared as v4_prepared, V4Runtime


def prepared(question='Which software release first supported this?'):
    envelope = deepcopy(v4_prepared(question)['context']['original'])
    text = envelope['sources'][0]['quote'].replace('. The', '.\n\nThe')
    envelope['sources'][0]['quote'] = text
    envelope['sources'][0]['context']['cited_chunk'] = {'quote':text,'selected_start':0,'selected_end':len(text)}
    return workflow.prepare(envelope,version=workflow.V4_VERSION)


def test_v3_source_binding_and_schema_admission_preserve_old_inventory():
    p = source.prepare(prepared()['context']['original'])
    raw = '{"classifications":{"C1":"protocol_version"},"type_context_units":["U2"]}'
    result = inventory.decode(raw, p)
    old = prior.decode(raw, p)
    assert {k:v for k,v in result.items() if k!='version'} == {k:v for k,v in old.items() if k!='version'}
    assert inventory.response_schema(p) == prior.response_schema(p)
    _structured_response_format(object.__new__(SplitModel), {'type':'json_object','schema':inventory.response_schema(p)})
    with pytest.raises(ValueError): inventory.prepare_match(p, old)
    match = inventory.prepare_match(p, result)
    assert inventory.match_decode('{"F1":"different_fact"}', match)['type_context_units'] == ['U2']
    forged = deepcopy(result); forged['facts'][0]['value'] = '9'
    with pytest.raises(ValueError): inventory.prepare_match(p, forged)


class V5Runtime(V4Runtime):
    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[1]['content']); self.calls.append((payload,kwargs))
        if payload['version']==context.VERSION:
            raw={'verdict':'not_stated','units':{u['id']:'background' for u in payload['units']},'dependencies':[],'comparisons':[]}
        elif payload['version']==inventory.VERSION:
            raw={'classifications':{'C1':'protocol_version'},'type_context_units':['U2']}
        else:
            assert payload['version']==inventory.MATCH_VERSION
            raw={'F1':'different_fact'}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(raw)}}]}


@pytest.mark.asyncio
async def test_v5_is_explicit_bound_and_preserves_absence_context():
    old = prepared(); envelope = old['context']['original']
    p = workflow.prepare(envelope,version=workflow.V5_VERSION)
    assert workflow.prepare(envelope,version=workflow.V4_VERSION)==old
    assert workflow.prepare(envelope)['version']!=workflow.V5_INPUT_VERSION
    runtime=V5Runtime(); attempt=await workflow.generate(p,runtime,model='model',model_sha='sha')
    assert attempt['status']=='proposal' and attempt['generation_calls']==3
    result=attempt['result'];assert result['version']==workflow.V5_VERSION
    assert result['component_versions']['inventory']==inventory.VERSION
    assert result['component_versions']['match']==inventory.MATCH_VERSION
    assert set(u['id'] for u in result['selected'])=={'U1','U2'}
    with pytest.raises(ValueError): workflow.compose(old,*(s['result'] for s in attempt['stages']))


def test_v3_scanner_stays_question_independent():
    a=source.prepare(prepared()['context']['original'])
    b=source.prepare(prepared('What is the identifier?')['context']['original'])
    assert inventory.messages(a)==inventory.messages(b)
