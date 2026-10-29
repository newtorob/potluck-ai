"""Opt-in profiles retain template, grammar, token protection and cancellation."""
import asyncio
import hashlib
import threading
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest
import llama_cpp
from llama_cpp import llama_chat_format

from potluck_local.services import investigation_profiles as profiles, prompt_guard
from potluck_local.services.runtime import LLMRuntime

TEMPLATE = "{{ '<|im_start|>user\\n' + messages[0].content + '<|im_end|>\\n<|im_start|>assistant\\n' }}{% if enable_thinking is defined and enable_thinking is false %}{{ '<think>\\n\\n</think>\\n\\n' }}{% endif %}"
FORMAT = {"type": "json_object", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}}


def fake_model(monkeypatch):
    profile = replace(profiles.resolve_profile(profiles.QWEN3_NON_THINKING), template_sha256=hashlib.sha256(TEMPLATE.encode()).hexdigest())
    monkeypatch.setattr(profiles, "_QWEN3", profile)
    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.verbose = False
    model.chat_handler = None
    model.chat_format = "chat_template.default"
    model.metadata = {"general.architecture": "qwen3", "tokenizer.chat_template": TEMPLATE}
    formatter = llama_chat_format.Jinja2ChatFormatter(TEMPLATE, eos_token="<|im_end|>", bos_token="", stop_token_ids=[9])
    model._chat_handlers = {model.chat_format: formatter.to_chat_handler()}
    model.tokenize = lambda text, add_bos=True, special=False: [1]
    return model


def reply():
    return {"id": "x", "created": 0, "model": "x", "object": "text_completion", "choices": [{"index": 0, "text": '{"answer":"ok"}', "finish_reason": "stop", "logprobs": None}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def test_profile_forwards_sampling_grammar_and_non_thinking_template(monkeypatch):
    model = fake_model(monkeypatch)
    seen = {}
    def tokenize(text, add_bos=True, special=False):
        seen["render"] = text.decode()
        return [1]
    model.tokenize = tokenize
    model.create_completion = lambda **kwargs: seen.update(kwargs) or reply()
    runtime = LLMRuntime()
    runtime._model = model
    asyncio.run(runtime.chat([{"role": "user", "content": "hello"}], response_format=FORMAT, generation_profile=profiles.QWEN3_NON_THINKING, seed=7))
    assert seen["render"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert (seen["temperature"], seen["top_p"], seen["top_k"], seen["min_p"], seen["seed"]) == (0.7, 0.8, 20, 0.0, 7)
    assert seen["repeat_penalty"] == 1.0
    assert isinstance(seen["grammar"], llama_cpp.LlamaGrammar)
    assert seen["stop"] == ["<|im_end|>"]
    assert len(seen["stopping_criteria"]) == 2


def test_rejects_unverified_handler_template_and_backend(monkeypatch):
    model = fake_model(monkeypatch)
    profile = profiles.resolve_profile(profiles.QWEN3_NON_THINKING)
    with pytest.raises(ValueError, match="Unknown"):
        profiles.resolve_profile("other")
    model.metadata["tokenizer.chat_template"] += "x"
    with pytest.raises(RuntimeError, match="template"):
        profiles.stock_handler(model, profile, threading.Event())
    model.metadata["tokenizer.chat_template"] = TEMPLATE
    model._chat_handlers[model.chat_format] = lambda **kwargs: None
    with pytest.raises(RuntimeError, match="handler"):
        profiles.stock_handler(model, profile, threading.Event())
    with pytest.raises(RuntimeError, match="handler"):
        profiles.stock_handler(object(), profile, threading.Event())


def test_guard_keeps_injected_control_tokens_as_plain_text(monkeypatch):
    from test_prompt_guard import FakeLlama, guard, INJECTION, IM_END
    model = fake_model(monkeypatch)
    model.calls = []
    model.SPECIAL = FakeLlama.SPECIAL
    model.tokenize = FakeLlama.tokenize.__get__(model, llama_cpp.Llama)
    prompt_guard.install_tokenize_guard(model, guard())
    model.create_completion = lambda **kw: reply()
    runtime = LLMRuntime()
    runtime._model = model
    asyncio.run(runtime.chat([{"role": "user", "content": INJECTION}], response_format=FORMAT, generation_profile=profiles.QWEN3_NON_THINKING))
    assert any(not special and IM_END.encode() in text for text, _, special in model.calls)


def test_cancellation_drains_after_stopping_criterion(monkeypatch):
    model = fake_model(monkeypatch)
    started = threading.Event()
    exited = threading.Event()
    def completion(**kw):
        started.set()
        while not kw["stopping_criteria"]([1], None):
            threading.Event().wait(.005)
        exited.set()
        return reply()
    model.create_completion = completion
    runtime = LLMRuntime()
    runtime._model = model
    async def exercise():
        task = asyncio.create_task(runtime.chat([{"role": "user", "content": "hello"}], response_format=FORMAT, generation_profile=profiles.QWEN3_NON_THINKING))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert exited.is_set()
        assert not runtime._inference_lock.locked()
    asyncio.run(exercise())


def test_installed_qwen3_template_if_available():
    path = Path.home() / ".potluck/models/qwen3-8b-q4/model.gguf"
    if not path.is_file():
        pytest.skip("installed Qwen3 GGUF unavailable")
    model = llama_cpp.Llama(model_path=str(path), vocab_only=True, verbose=False)
    handler = profiles.stock_handler(model, profiles.resolve_profile(profiles.QWEN3_NON_THINKING), threading.Event())
    closure = dict(zip(handler.__code__.co_freevars, handler.__closure__))
    result = closure["chat_formatter"].cell_contents(messages=[{"role": "user", "content": "hello"}])
    assert result.prompt.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_generic_structured_cancellation_keeps_stock_sampling_defaults(monkeypatch):
    model = fake_model(monkeypatch)
    started = threading.Event()
    seen = {}
    def completion(**kwargs):
        seen.update(kwargs)
        started.set()
        while not kwargs['stopping_criteria']([1], None):
            threading.Event().wait(.005)
        return reply()
    model.create_completion = completion
    runtime = LLMRuntime()
    runtime._model = model
    async def exercise():
        task = asyncio.create_task(runtime.chat([{'role': 'user', 'content': 'hello'}],
            temperature=0, response_format=FORMAT, bounded_generation=True))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    asyncio.run(exercise())
    assert (seen['temperature'], seen['top_p'], seen['top_k'], seen['min_p'], seen['repeat_penalty']) == (0, .95, 40, .05, 1.0)


def test_preflight_profile_checks_loaded_formatter_without_inference(monkeypatch):
    model = fake_model(monkeypatch)
    runtime = LLMRuntime()
    runtime._model = model
    metadata = asyncio.run(runtime.validate_investigation_profile(profiles.QWEN3_NON_THINKING))
    assert metadata['enable_thinking'] is False
    model.metadata['tokenizer.chat_template'] += 'bad'
    with pytest.raises(ValueError, match='incompatible'):
        asyncio.run(runtime.validate_investigation_profile(profiles.QWEN3_NON_THINKING))


def test_installed_compiler_grammar_enforces_bounds_if_vocab_available():
    import ctypes
    import json
    import math
    path = Path.home() / '.potluck/models/qwen3-8b-q4/model.gguf'
    if not path.is_file():
        pytest.skip('installed Qwen3 GGUF unavailable')
    model = llama_cpp.Llama(model_path=str(path), vocab_only=True, verbose=False)
    schema = {'type': 'object', 'properties': {
        'items': {'type': 'array', 'minItems': 1, 'maxItems': 2,
                  'items': {'type': 'string', 'minLength': 1, 'maxLength': 2}},
        'status': {'type': 'string', 'enum': ['yes', 'no']}},
        'required': ['items', 'status'], 'additionalProperties': False}
    grammar = llama_cpp.LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)
    def accepts(value):
        sampler = llama_cpp.llama_sampler_init_grammar(model._model.vocab, grammar._grammar.encode(), b'root')
        try:
            tokens = model.tokenize(value.encode(), add_bos=False, special=False) + [model.token_eos()]
            for token in tokens:
                data = (llama_cpp.llama_token_data * 1)(llama_cpp.llama_token_data(token, 1., 0.))
                candidates = llama_cpp.llama_token_data_array(data, 1, -1, False)
                llama_cpp.llama_sampler_apply(sampler, ctypes.byref(candidates))
                if math.isinf(data[0].logit):
                    return False
                llama_cpp.llama_sampler_accept(sampler, token)
            return True
        finally:
            llama_cpp.llama_sampler_free(sampler)
    assert accepts('{"items":["a","ab"],"status":"yes"}')
    assert not accepts('{"items":[],"status":"yes"}')
    assert not accepts('{"items":["a","a","a"],"status":"yes"}')
    assert not accepts('{"items":[""],"status":"yes"}')
    assert not accepts('{"items":["abc"],"status":"yes"}')
    assert not accepts('{"items":["a"],"status":"maybe"}')


@pytest.mark.parametrize('bounds', [
    {'minLength': -1}, {'maxLength': True}, {'minLength': 3, 'maxLength': 2},
    {'enum': ['ab'], 'maxLength': 1},
])
def test_invalid_or_silently_ignored_string_bounds_are_rejected(bounds):
    from potluck_local.services.runtime import _structured_response_format
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string', **bounds}},
              'required': ['answer'], 'additionalProperties': False}
    with pytest.raises(ValueError):
        _structured_response_format(object(), {'type': 'json_object', 'schema': schema})
