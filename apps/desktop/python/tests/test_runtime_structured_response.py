"""Structured generation must either retain its schema or fail before decode."""
import asyncio
import json
from contextlib import ExitStack

import pytest

from potluck_local.services.runtime import LLMRuntime
from potluck_local.services.split_native import SplitModel

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
FORMAT = {"type": "json_object", "schema": SCHEMA}


def request(model, **kwargs):
    runtime = LLMRuntime()
    runtime._model = model
    return asyncio.run(runtime.chat([{"role": "user", "content": "test"}], **kwargs))


def test_stock_handler_receives_compiled_schema_grammar(monkeypatch):
    import llama_cpp
    from llama_cpp import llama_chat_format

    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.chat_handler = None
    model.chat_format = "example"
    model._chat_handlers = {"example": llama_chat_format.chat_formatter_to_chat_completion_handler(lambda **kw: None)}
    calls = []
    sentinel = object()
    monkeypatch.setattr(llama_cpp.LlamaGrammar, "from_json_schema", lambda schema, **kw: calls.append(json.loads(schema)) or sentinel)
    model.create_chat_completion = lambda **kw: calls.append(kw) or {"choices": []}
    request(model, response_format=FORMAT)
    assert calls[0] == SCHEMA
    assert calls[1]["grammar"] is sentinel
    assert "response_format" not in calls[1]


def test_schema_compilation_error_does_not_generate(monkeypatch):
    import llama_cpp
    from llama_cpp import llama_chat_format

    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.chat_handler = None
    model.chat_format = "example"
    model._chat_handlers = {"example": llama_chat_format.chat_formatter_to_chat_completion_handler(lambda **kw: None)}
    model.create_chat_completion = lambda **kw: pytest.fail("generation must not run")
    def fail(*args, **kwargs):
        raise ValueError("bad grammar")
    monkeypatch.setattr(llama_cpp.LlamaGrammar, "from_json_schema", fail)
    with pytest.raises(ValueError, match="bad grammar"):
        request(model, response_format=FORMAT)


def test_native_receives_response_format():
    model = object.__new__(SplitModel)
    calls = []
    model.create_chat_completion = lambda **kw: calls.append(kw) or {"choices": []}
    request(model, response_format=FORMAT)
    assert calls[0]["response_format"] == FORMAT


def test_unsupported_backend_and_custom_handler_fail_closed():
    class Unsupported:
        def create_chat_completion(self, **kwargs):
            pytest.fail("generation must not run")
    with pytest.raises(RuntimeError, match="unavailable"):
        request(Unsupported(), response_format=FORMAT)
    import llama_cpp
    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.chat_handler = lambda **kwargs: None
    with pytest.raises(RuntimeError, match="unavailable"):
        request(model, response_format=FORMAT)


@pytest.mark.parametrize("schema", [None, {}, {"type": "object"},
    {**SCHEMA, "$ref": "#/$defs/x"}, {**SCHEMA, "required": ["missing"]},
    {**SCHEMA, "additionalProperties": True},
    {**SCHEMA, "properties": {"answer": {"type": "string", "minLength": -1}}}])
def test_invalid_schema_rejected(schema):
    model = object.__new__(SplitModel)
    model.create_chat_completion = lambda **kw: pytest.fail("generation must not run")
    with pytest.raises(ValueError):
        request(model, response_format={"type": "json_object", "schema": schema})


def test_tools_incompatible():
    model = object.__new__(SplitModel)
    model.create_chat_completion = lambda **kw: pytest.fail("generation must not run")
    with pytest.raises(ValueError, match="cannot be combined"):
        request(model, response_format=FORMAT, tools=[{"type": "function"}])


def test_existing_unstructured_calls_remain_unchanged():
    class Model:
        def create_chat_completion(self, **kwargs):
            assert "response_format" not in kwargs and "grammar" not in kwargs
            return {"choices": []}
    request(Model())


@pytest.mark.parametrize("injected", [False, True])
def test_native_adapter_forwards_schema_with_and_without_guard(monkeypatch, injected):
    from test_split_native_guard import build, INJECTED
    model, engine, _ = build(monkeypatch, reply='{\"answer\":\"ok\"}')
    runtime = LLMRuntime()
    runtime._model = model
    content = INJECTED if injected else "hello"
    asyncio.run(runtime.chat([{"role": "user", "content": content}], response_format=FORMAT))
    if injected:
        body = engine.body("/v1/completions")
        assert body["json_schema"] == SCHEMA
        assert "response_format" not in body
    else:
        assert engine.body("/v1/chat/completions")["response_format"] == FORMAT


def test_installed_stock_formatter_preserves_explicit_grammar():
    from types import SimpleNamespace
    from llama_cpp import LlamaGrammar, llama_chat_format

    grammar = LlamaGrammar.from_json_schema(json.dumps(SCHEMA), verbose=False)
    received = []
    class FakeLlama:
        verbose = False
        def tokenize(self, *args, **kwargs):
            return [1]
        def create_completion(self, **kwargs):
            received.append(kwargs)
            return {"id": "x", "created": 0, "model": "x", "object": "text_completion",
                    "choices": [{"index": 0, "text": '{"answer":"ok"}', "finish_reason": "stop", "logprobs": None}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    handler = llama_chat_format.chat_formatter_to_chat_completion_handler(
        lambda **kw: SimpleNamespace(prompt="test", added_special=False, stop=None, stopping_criteria=None))
    handler(llama=FakeLlama(), messages=[], grammar=grammar)
    assert received[0]["grammar"] is grammar


def test_stock_guarded_injection_retains_schema_grammar(monkeypatch):
    from types import SimpleNamespace
    import llama_cpp
    from llama_cpp import llama_chat_format
    from potluck_local.services import prompt_guard
    from test_prompt_guard import FakeLlama, guard, chatml, INJECTION, IM_END

    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.verbose = False
    model.chat_handler = None
    model.chat_format = "chat_template.default"
    model.calls = []
    model.tokenize = FakeLlama.tokenize.__get__(model, llama_cpp.Llama)
    model.SPECIAL = FakeLlama.SPECIAL
    prompt_guard.install_tokenize_guard(model, guard())
    seen = []
    def formatter(**kwargs):
        seen.append(kwargs["messages"])
        return SimpleNamespace(prompt=chatml(kwargs["messages"]), added_special=False,
                               stop=None, stopping_criteria=None)
    model._chat_handlers = {model.chat_format: llama_chat_format.chat_formatter_to_chat_completion_handler(formatter)}
    def completion(**kwargs):
        seen.append(kwargs)
        return {"id": "x", "created": 0, "model": "x", "object": "text_completion",
                "choices": [{"index": 0, "text": '{"answer":"ok"}', "finish_reason": "stop", "logprobs": None}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    model.create_completion = completion
    monkeypatch.setattr(llama_chat_format, "_grammar_for_response_format",
                        lambda *args, **kwargs: pytest.fail("must not use generic JSON fallback"))
    runtime = LLMRuntime()
    runtime._model = model
    asyncio.run(runtime.chat([{"role": "user", "content": INJECTION}], response_format=FORMAT))
    assert IM_END not in seen[0][0]["content"]
    assert isinstance(seen[1]["grammar"], llama_cpp.LlamaGrammar)
    assert seen[1]["prompt"].count(FakeLlama.SPECIAL[IM_END]) == 1
    assert any(not special and IM_END.encode() in text for text, _, special in model.calls)


def test_unknown_registered_chat_handler_is_rejected():
    import llama_cpp
    model = object.__new__(llama_cpp.Llama)
    model._stack = ExitStack()
    model.chat_handler = None
    model.chat_format = "custom"
    model._chat_handlers = {"custom": lambda **kwargs: pytest.fail("generation must not run")}
    with pytest.raises(RuntimeError, match="chat handler"):
        request(model, response_format=FORMAT)
