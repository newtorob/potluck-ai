import asyncio
from copy import deepcopy

import pytest

from potluck_local.services import investigation_evidence_revision_service as service
from potluck_local.services import investigations, prompt_cache


@pytest.fixture
def prepared(monkeypatch):
    value = {'version': service.contract.VERSION, 'units': [{'id': 'U1'}]}
    monkeypatch.setattr(service.contract, 'response_schema', lambda _: {'type': 'object'})
    monkeypatch.setattr(service.contract, 'messages', lambda _: [
        {'role': 'user', 'content': 'Bound source units.'}])
    monkeypatch.setattr(service.contract, 'decode', lambda raw, _: (
        {'kind': 'unverified', 'needs_review': True} if raw == 'valid' else _invalid()))
    return value


def _invalid():
    raise ValueError('Invalid output')


class Runtime:
    def __init__(self, *, capacity=8192, finish='stop', text='valid', failure=None,
                 changed=False):
        self.capacity, self.finish, self.text = capacity, finish, text
        self.failure, self.changed = failure, changed
        self.calls = []

    def current_model_id(self):
        return 'other' if self.changed and self.calls else 'model'

    def current_model_sha256(self):
        return 'sha'

    async def validate_investigation_profile(self, *args, **kwargs):
        assert kwargs == {'expected_model': 'model', 'expected_sha256': 'sha'}

    async def measure_investigation_prompt(self, messages, **kwargs):
        return {'input_tokens': 1000, 'context_tokens': self.capacity}

    async def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        assert prompt_cache.request_is_local.get() is False
        if self.failure:
            raise self.failure
        return {'choices': [{'finish_reason': self.finish,
                             'message': {'content': self.text}}]}


@pytest.mark.asyncio
async def test_one_call_is_bound_and_detached(prepared):
    before = deepcopy(prepared)
    runtime = Runtime()
    result = await service.generate(prepared, runtime, model='model', model_sha='sha', seed=11)
    assert prepared == before
    assert result['status'] == 'proposal' and result['generation_calls'] == 1
    assert result['result']['needs_review'] is True
    kwargs = runtime.calls[0][1]
    assert kwargs['seed'] == 11 and kwargs['max_tokens'] == 700
    assert kwargs['expected_context_tokens'] == 8192
    assert kwargs['expected_model'] == 'model' and kwargs['expected_sha256'] == 'sha'
    assert kwargs['bounded_generation'] is True
    result['input']['units'].clear()
    assert prepared == before


@pytest.mark.parametrize(('options', 'reason', 'calls'), [
    ({'capacity': 1699}, 'capacity', 0),
    ({'capacity': True}, 'inference_failed', 0),
    ({'finish': 'length'}, 'invalid_output', 1),
    ({'text': 'bad'}, 'invalid_output', 1),
    ({'changed': True}, 'model_changed', 1),
    ({'failure': TimeoutError()}, 'timeout', 1),
    ({'failure': RuntimeError('transport failed')}, 'inference_failed', 1),
])
@pytest.mark.asyncio
async def test_failure_never_retries_or_returns_a_proposal(prepared, options, reason, calls):
    runtime = Runtime(**options)
    result = await service.generate(prepared, runtime, model='model', model_sha='sha')
    assert result['reason'] == reason and result['status'] == 'not_generated'
    assert result['generation_calls'] == len(runtime.calls) == calls
    assert 'result' not in result


@pytest.mark.asyncio
async def test_cancellation_propagates_and_restores_context(prepared):
    previous = prompt_cache.request_is_local.get()
    with pytest.raises(asyncio.CancelledError):
        await service.generate(prepared, Runtime(failure=asyncio.CancelledError()),
                               model='model', model_sha='sha')
    assert prompt_cache.request_is_local.get() == previous


@pytest.mark.parametrize('seed', [True, -1, 4294967296, '7'])
@pytest.mark.asyncio
async def test_invalid_seed_never_calls_model(prepared, seed):
    runtime = Runtime()
    with pytest.raises(ValueError, match='seed'):
        await service.generate(prepared, runtime, model='model', model_sha='sha', seed=seed)
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_identity_mismatch_fails_before_inference(prepared):
    runtime = Runtime()
    with pytest.raises(ValueError, match='loaded locally'):
        await service.generate(prepared, runtime, model='different', model_sha='sha')
    assert runtime.calls == []


@pytest.mark.parametrize('changed', ['status', 'brief', 'evidence', 'provenance', 'review',
                                    'model_slug', 'model_sha', 'scope', None])
@pytest.mark.asyncio
async def test_preview_rechecks_scope_and_original_after_inference(monkeypatch, changed):
    original = {'status': 'draft', 'brief': 'Original.', 'evidence': [], 'provenance': {},
                'review': None, 'model_slug': 'model', 'model_sha': 'sha'}
    later = deepcopy(original)
    if changed and changed != 'scope':
        later[changed] = 'changed'
    calls = []

    async def get(scope, iid):
        calls.append((scope, iid))
        if len(calls) == 2 and changed == 'scope':
            raise ValueError('Out of scope')
        return deepcopy(original if len(calls) == 1 else later)

    async def generate(*args, **kwargs):
        return {'status': 'proposal', 'result': {'needs_review': True}}

    monkeypatch.setattr(investigations, 'get_investigation', get)
    monkeypatch.setattr(investigations, 'get_runtime', lambda: object())
    monkeypatch.setattr(service.original_revision, 'prepare_original',
                        lambda _: [{'group': {'id': 'G1'}}])
    monkeypatch.setattr(service.contract, 'prepare', lambda e: e)
    monkeypatch.setattr(service, 'generate', generate)
    if changed:
        with pytest.raises(ValueError):
            await service.propose('scoped', 'original-id', 'G1')
    else:
        result = await service.propose('scoped', 'original-id', 'G1')
        assert result['original_sha256'] == service.common.digest(original)
        assert result['attempt']['result']['needs_review'] is True
    assert calls == [('scoped', 'original-id')] * 2


@pytest.mark.asyncio
async def test_reviewed_original_rejected_before_preparation(monkeypatch):
    async def get(*args):
        return {'status': 'accepted'}
    monkeypatch.setattr(investigations, 'get_investigation', get)
    monkeypatch.setattr(service.original_revision, 'prepare_original', lambda _: pytest.fail())
    with pytest.raises(ValueError, match='unreviewed'):
        await service.propose('scope', 'id', 'G1')
