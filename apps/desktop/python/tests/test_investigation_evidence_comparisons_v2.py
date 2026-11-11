import asyncio
from copy import deepcopy
import json
import pytest

from potluck_local.services import investigation_evidence_comparisons as v1
from potluck_local.services import investigation_evidence_comparisons_v2 as v2
from potluck_local.services import investigation_evidence_comparisons_service as service
from test_investigation_evidence_revision import sample


def source(single=False):
    result = sample(); result['single_focus'] = single
    result['group']['request'] = 'What is the rule and who approves it?'
    result['group']['focus'] = 'Approval'
    return result


def response(prepared):
    return {'verdict': 'source_rule', 'units': {u['id']: ('answer' if i == 0 else 'background')
            for i, u in enumerate(prepared['units'])}, 'dependencies': [], 'comparisons': []}


def test_single_request_differs_only_in_version():
    envelope = source(single=True); before = deepcopy(envelope)
    one, two = v1.prepare(envelope), v2.prepare(envelope)
    a, b = v1.messages(one), v2.messages(two)
    first, second = json.loads(a[1]['content']), json.loads(b[1]['content'])
    assert first.pop('version') == v1.VERSION and second.pop('version') == v2.VERSION
    assert first == second and a[0] == b[0] and v1.response_schema(one) == v2.response_schema(two)
    assert envelope == before


def test_selected_group_gets_exact_benchmark_instruction_and_full_question():
    one, two = v1.prepare(source()), v2.prepare(source())
    a, b = v1.messages(one), v2.messages(two)
    assert b[0]['content'] == a[0]['content'] + v2.SCOPE_INSTRUCTION
    first, second = json.loads(a[1]['content']), json.loads(b[1]['content'])
    assert second.pop('original_question') == two['selection_scope']['full_question']
    first.pop('version'); second.pop('version'); assert first == second
    assert 'draft_sentences' not in b[1]['content'] and 'status' not in b[1]['content']


def test_result_binds_v2_without_mutating_input_and_rejects_wrong_versions():
    prepared = v2.prepare(source()); before = deepcopy(prepared); raw = json.dumps(response(prepared))
    result = v2.decode(raw, prepared)
    assert result['version'] == v2.VERSION and result['input_sha256'] == v1.common.digest(prepared)
    assert result['selection_scope'] == prepared['selection_scope'] and result['selected'][0]['quote'] == prepared['units'][0]['quote']
    assert prepared == before
    with pytest.raises(ValueError): v2.decode(raw, v1.prepare(source()))
    bad = deepcopy(prepared); bad['version'] = 'unknown'
    with pytest.raises(ValueError): v2.messages(bad)


class Runtime:
    def __init__(self): self.started = 0; self.both = asyncio.Event(); self.messages = []
    def current_model_id(self): return 'model'
    def current_model_sha256(self): return 'sha'
    async def validate_investigation_profile(self, *args, **kwargs): pass
    async def measure_investigation_prompt(self, messages, **kwargs): return {'input_tokens': 100, 'context_tokens': 8192}
    async def chat(self, messages, **kwargs):
        self.messages.append(deepcopy(messages)); self.started += 1
        if self.started == 2: self.both.set()
        await self.both.wait()
        payload = json.loads(messages[1]['content'])
        raw = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if i == 0 else 'background')
                for i,u in enumerate(payload['units'])}, 'dependencies': [], 'comparisons': []}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}


@pytest.mark.asyncio
async def test_concurrent_generations_keep_own_version_and_prompt():
    runtime = Runtime(); one, two = v1.prepare(source()), v2.prepare(source())
    a, b = await asyncio.gather(service.generate(one, runtime, model='model', model_sha='sha'),
                                service.generate(two, runtime, model='model', model_sha='sha'))
    assert a['status'] == b['status'] == 'proposal'
    assert a['result']['version'] == a['version'] == v1.VERSION
    assert b['result']['version'] == b['version'] == v2.VERSION
    payloads = {json.loads(m[1]['content'])['version']: json.loads(m[1]['content']) for m in runtime.messages}
    assert 'original_question' not in payloads[v1.VERSION]
    assert payloads[v2.VERSION]['original_question'] == two['selection_scope']['full_question']


@pytest.mark.asyncio
async def test_unknown_version_rejected_before_inference_and_cancel_propagates():
    runtime = Runtime(); bad = v2.prepare(source()); bad['version'] = 'unknown'
    with pytest.raises(ValueError): await service.generate(bad, runtime, model='model', model_sha='sha')
    assert runtime.started == 0
    task = asyncio.create_task(service.generate(v2.prepare(source()), runtime, model='model', model_sha='sha'))
    while not runtime.started: await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task


from test_investigation_verification import setup
from potluck_local.services import investigations as inv


@pytest.mark.asyncio
async def test_explicit_v2_and_default_v1_proposals_preserve_real_record(setup):
    runtime, scope, original, _ = await setup()
    async def chat(messages, **kwargs):
        payload = json.loads(messages[1]['content']); runtime.calls.append(payload)
        raw = {'verdict': 'source_rule', 'units': {u['id']: ('answer' if i == 0 else 'background')
                for i, u in enumerate(payload['units'])}, 'dependencies': [], 'comparisons': []}
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]}
    runtime.chat = chat
    with pytest.raises(ValueError): await service.propose(scope, original['id'], 'G1', version='unknown')
    assert runtime.calls == []
    first = await service.propose(scope, original['id'], 'G1')
    second = await service.propose(scope, original['id'], 'G1', version=v2.VERSION)
    assert first['attempt']['result']['version'] == v1.VERSION
    assert second['attempt']['result']['version'] == v2.VERSION
    assert await inv.get_investigation(scope, original['id']) == original
