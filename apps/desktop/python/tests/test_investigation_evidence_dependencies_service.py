import asyncio
import json
from copy import deepcopy
import pytest
from potluck_local.services import investigation_evidence_dependencies as dep
from potluck_local.services import investigation_evidence_dependencies_service as service
from potluck_local.services import investigations as inv
from potluck_local.services.evidence_lookup import EvidenceScope
from potluck_local.services.source_collections import scan_collection
from test_investigation_evidence_dependencies import prepared, answer
from test_investigation_verification import setup


class Runtime:
    def __init__(self, mode='ok'):
        self.mode = mode; self.calls = 0
    def current_model_id(self): return 'other' if self.mode == 'changed' and self.calls else 'model'
    def current_model_sha256(self): return 'sha'
    async def validate_investigation_profile(self, *args, **kwargs): pass
    async def measure_investigation_prompt(self, *args, **kwargs):
        return {'input_tokens': 1000, 'context_tokens': 1699 if self.mode == 'capacity' else 8192}
    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.mode == 'cancel': raise asyncio.CancelledError()
        data = json.loads(messages[1]['content']); uid = data['units'][0]['id']
        reply = {'verdict': 'not_stated', 'units': {u['id']: ('distinction' if u['id'] == uid else 'background') for u in data['units']},
                 'dependencies': [], 'distinctions': []}
        return {'choices': [{'finish_reason': 'length' if self.mode == 'length' else 'stop', 'message': {'content': json.dumps(reply)}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'status', 'reason', 'calls'), [('ok','proposal',None,1), ('capacity','not_generated','capacity',0),
    ('changed','not_generated','model_changed',1), ('length','not_generated','invalid_output',1)])
async def test_bounded_generation_and_pin(mode, status, reason, calls):
    runtime = Runtime(mode); inp = prepared(); before = deepcopy(inp)
    result = await service.generate(inp, runtime, model='model', model_sha='sha')
    assert (result['status'], result['reason'], runtime.calls) == (status, reason, calls)
    assert inp == before
    with pytest.raises(ValueError): await service.generate(inp, runtime, model='wrong', model_sha='sha')
    assert runtime.calls == calls


@pytest.mark.asyncio
async def test_cancel_propagates():
    runtime = Runtime('cancel')
    with pytest.raises(asyncio.CancelledError): await service.generate(prepared(), runtime, model='model', model_sha='sha')
    assert runtime.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['none', 'review', 'rescan'])
async def test_scoped_real_database_lifecycle(setup, race):
    runtime, scope, original, path = await setup()
    with pytest.raises(LookupError): await service.propose(EvidenceScope('other', scope.collection_ids), original['id'], 'G1')
    assert runtime.calls == []
    async def chat(messages, **kwargs):
        runtime.calls.append((messages, kwargs)); data = json.loads(messages[1]['content'])
        if race == 'review': await inv.review_investigation(scope, original['id'], 'accept', 'owner', 'Reviewed.')
        if race == 'rescan':
            path.write_text('# Changed\n\nDifferent source.\n'); await scan_collection('w', scope.collection_ids[0])
        result = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if i == 0 else 'background') for i,u in enumerate(data['units'])},
                  'dependencies': [], 'distinctions': []}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}
    runtime.chat = chat
    if race == 'none':
        result = await service.propose(scope, original['id'], 'G1')
        assert result['attempt']['status'] == 'proposal' and await inv.get_investigation(scope, original['id']) == original
    else:
        with pytest.raises((LookupError, ValueError)): await service.propose(scope, original['id'], 'G1')
    assert len(runtime.calls) == 1
