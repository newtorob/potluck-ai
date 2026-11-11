"""Native local backend contracts, without downloading weights or starting inference."""
import asyncio
import hashlib
import json
import threading
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from potluck_local.services import local_server as ns
from potluck_local.services import investigation_profiles as profiles
from potluck_local.services.acceptance import lending_allowed
from potluck_local.services.model_identity import ModelIdentityError
from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.runtime import LLMRuntime
from potluck_local.services.settings_manager import RuntimeSettings


class Process:
    def __init__(self, *args, **kwargs):
        self.process = self
        self.closed = False

    def poll(self):
        return 0 if self.closed else None

    def heartbeat(self):
        if self.closed:
            raise RuntimeError("stopped")

    def close(self):
        self.closed = True


@pytest.fixture
def server(monkeypatch):
    props = {"default_generation_settings": {"n_ctx": 4096}, "total_slots": 1,
             "chat_template_caps": {"supports_tools": True, "supports_tool_calls": True},
             "chat_template": "enable_thinking <think>"}
    seen = []
    state = {"prompt": "tool: potluck_probe_zq91", "plain_prompt": "hi<think>", "busy": False}
    client = httpx._client.Client

    def handle(request):
        seen.append(request)
        if request.url.path == "/props":
            return httpx.Response(200, json=props)
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"is_processing": state["busy"]}])
        if request.url.path == "/apply-template":
            key = "prompt" if "tools" in json.loads(request.content) else "plain_prompt"
            return httpx.Response(200, json={"prompt": state[key]})
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(ns.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    model = ns.LocalServerModel(Process(), 19999, "private-test-key")
    yield model, props, state, seen
    model.close()


def test_native_renderer_establishes_tools_and_reasoning_contract(server):
    model, _, _, seen = server
    model.initialize(4096)
    assert model.template_traits == TemplateTraits(supports_tools=True, opens_reasoning=True, thinks=True)
    assert all(r.headers["authorization"] == "Bearer private-test-key" for r in seen)
    assert json.loads(seen[-2].content)["tools"][0] == ns.PROBE_TOOL


def test_investigation_template_identity_is_from_loaded_renderer(server):
    model, props, _, _ = server
    model.initialize(4096)
    assert model.investigation_template_sha256 == hashlib.sha256(props["chat_template"].encode()).hexdigest()
    props["chat_template"] = ""
    model.initialize(4096)
    assert model.investigation_template_sha256 is None


def test_registered_native_profile_has_exact_pins_and_preserves_stock_profile():
    native = profiles.resolve_profile(profiles.QWEN38_27B_NATIVE_NON_THINKING)
    assert isinstance(native, profiles.NativeInvestigationProfile)
    assert native.backend == 'llama-server' and native.enable_thinking is False
    assert native.model_sha256 == '96457164fd300e37e8cc4c611ad5f8f849db51e2d3b29bbede98012c724b90e3'
    assert native.engine_commit == 'd80619397c2ba5cf1366c29f7d43699ca3a159b9'
    assert native.template_sha256 == 'c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041'
    assert native.render_hashes == (
        '9f81e9d717249bef5fc543518009ba0e427864fbe56fe8c3e43db5a5a1fb8092',
        '0a7393b3d4d034f93bf43512915604111ad1b8ac27edf386602eaf776556b061')
    stock = profiles.resolve_profile(profiles.QWEN3_NON_THINKING)
    assert stock.backend == 'llama-cpp-python' and not isinstance(stock, profiles.NativeInvestigationProfile)
    assert set(stock.metadata()) == {'name', 'temperature', 'top_p', 'top_k', 'min_p',
                                    'template_sha256', 'backend', 'enable_thinking', 'repeat_penalty'}


def test_native_profile_requires_exact_identity_and_switch_render(server, monkeypatch):
    model, _, _, _ = server
    model.initialize(4096)
    hashes = tuple(hashlib.sha256(s.encode()).hexdigest() for s in ("off", "on"))
    profile = profiles.NativeInvestigationProfile("test", .7, .8, 20, 0,
        model.investigation_template_sha256, backend="llama-server", model_sha256="sha",
        engine_commit="commit", render_hashes=hashes)
    client = httpx._client.Client
    requests = []
    def render(request):
        body = json.loads(request.content)
        requests.append(body)
        enabled = body["chat_template_kwargs"]["enable_thinking"]
        return httpx.Response(200, json={"prompt": "on" if enabled else "off"})
    monkeypatch.setattr(ns.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(render), **kw))
    options = dict(model_sha256="sha", engine_commit="commit")
    profiles.validate_native(model, profile, **options)
    assert [r["chat_template_kwargs"]["enable_thinking"] for r in requests] == [False, True]
    for key in ("model_sha256", "engine_commit"):
        with pytest.raises(RuntimeError):
            profiles.validate_native(model, profile, **{**options, key: "wrong"})
    with pytest.raises(RuntimeError):
        profiles.validate_native(model, replace(profile, template_sha256="wrong"), **options)
    with pytest.raises(RuntimeError):
        profiles.validate_native(model, replace(profile, enable_thinking=True), **options)
    with pytest.raises(RuntimeError):
        profiles.validate_native(model, replace(profile, render_hashes=(hashes[0], hashes[0])), **options)
    with pytest.raises(RuntimeError):
        profiles.validate_native(model, replace(profile, render_hashes=hashes[::-1]), **options)


def test_native_profile_runtime_forwards_schema_and_sampling_without_python_binding(server, monkeypatch):
    import builtins
    model, _, _, _ = server
    profile = profiles.NativeInvestigationProfile("test-native", .7, .8, 20, 0, "template",
        backend="llama-server", model_sha256="sha", engine_commit="commit", render_hashes=("off", "on"))
    monkeypatch.setattr(profiles, "resolve_profile", lambda name: profile)
    monkeypatch.setattr(profiles, "validate_native", lambda *a, **kw: None)
    original_import = builtins.__import__
    def no_binding(name, *args, **kwargs):
        if name.startswith("llama_cpp"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_binding)
    seen = {}
    model.create_chat_completion = lambda **kw: seen.update(kw) or {"choices": [{"finish_reason": "stop"}]}
    runtime = LLMRuntime()
    runtime._model = model
    runtime._load_report = {"engine_commit": "commit"}
    monkeypatch.setattr(runtime, "current_model_sha256", lambda: "sha")
    schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    fmt = {"type": "json_object", "schema": schema}
    async def exercise():
        metadata = await runtime.validate_investigation_profile("test-native")
        assert metadata["backend"] == "llama-server" and metadata["model_sha256"] == "sha"
        await runtime.chat([{"role": "user", "content": "hi"}], generation_profile="test-native",
                           response_format=fmt, seed=7)
        with pytest.raises(ValueError):
            await runtime.chat([{"role": "user", "content": "hi"}], generation_profile="test-native",
                               response_format=fmt, tools=[{"type": "function"}])
    asyncio.run(exercise())
    assert seen["response_format"] == fmt
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["_potluck_investigation_completion"] is True
    assert (seen["temperature"], seen["top_p"], seen["top_k"], seen["min_p"],
            seen["repeat_penalty"], seen["seed"]) == (.7, .8, 20, 0, 1.0, 7)


def test_cancelled_native_profile_preflight_drains_before_unlock(server, monkeypatch):
    model, _, _, _ = server
    profile = profiles.NativeInvestigationProfile("test-native", .7, .8, 20, 0, "template",
        backend="llama-server", model_sha256="sha", engine_commit="commit", render_hashes=("off", "on"))
    monkeypatch.setattr(profiles, "resolve_profile", lambda name: profile)
    started, release = threading.Event(), threading.Event()
    def validate(*args, **kwargs):
        started.set()
        assert release.wait(2)
    monkeypatch.setattr(profiles, "validate_native", validate)
    runtime = LLMRuntime()
    runtime._model = model
    monkeypatch.setattr(runtime, "current_model_sha256", lambda: "sha")
    async def exercise():
        task = asyncio.create_task(runtime.validate_investigation_profile("test-native"))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(.01)
        assert runtime._inference_lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runtime._inference_lock.locked()
    asyncio.run(exercise())


def test_python_profile_is_rejected_on_native_without_import(server, monkeypatch):
    model, _, _, _ = server
    runtime = LLMRuntime()
    runtime._model = model
    monkeypatch.setattr(profiles, "stock_handler", lambda *a: pytest.fail("Python handler called"))
    async def exercise():
        with pytest.raises(ValueError, match="Python backend"):
            await runtime.validate_investigation_profile(profiles.QWEN3_NON_THINKING)
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["capability", "render", "calls"])
def test_never_advertises_tools_the_engine_cannot_render(server, change):
    model, props, state, _ = server
    if change == "render":
        state["prompt"] = "hi"
    else:
        key = "supports_tools" if change == "capability" else "supports_tool_calls"
        props["chat_template_caps"][key] = False
    model.initialize(4096)
    assert not model.template_traits.supports_tools


def test_native_reasoning_uses_rendered_prompt_instead_of_template_text(server):
    model, props, state, _ = server
    state["plain_prompt"] = "assistant: "
    model.initialize(4096)
    assert not model.template_traits.opens_reasoning and model.template_traits.thinks
    props["chat_template"] = "ordinary template"
    model.initialize(4096)
    assert not model.template_traits.thinks


@pytest.mark.parametrize("context,slots", [(2048, 1), (None, 1), ("4096", 1), (4096, 2)])
def test_rejects_lower_unknown_or_divided_context(server, context, slots):
    model, props, _, _ = server
    props["default_generation_settings"]["n_ctx"] = context
    props["total_slots"] = slots
    with pytest.raises(ns.SplitError) as error:
        model.initialize(4096)
    assert error.value.code == "native_context_mismatch"


def test_native_start_timeout_cleans_up_process(server, monkeypatch, tmp_path):
    monkeypatch.setattr(ns, "free_port", lambda: 19999)
    process = Process()
    monkeypatch.setattr(ns, "NativeProcess", lambda *a, **kw: process)
    monkeypatch.setattr(ns, "LOAD_TIMEOUT", 0)
    with pytest.raises(ns.SplitError) as error:
        ns.start("model.gguf", root=tmp_path, n_ctx=4096, n_gpu_layers=0, n_threads=2, cache_mib=0)
    assert error.value.code == "native_load_timeout" and process.closed


def test_engine_start_is_private_bounded_and_does_not_use_shell(server, monkeypatch, tmp_path):
    monkeypatch.setattr(ns, "free_port", lambda: 19999)
    calls = []
    def spawn(args, **kw):
        calls.append((args, kw))
        return Process()
    monkeypatch.setattr(ns, "NativeProcess", spawn)
    model = ns.start("a model.gguf", root=tmp_path, n_ctx=4096, n_gpu_layers=0, n_threads=2, cache_mib=128)
    try:
        args, kw = calls[0]
        assert args[args.index("--host") + 1] == "127.0.0.1"
        assert args[args.index("--model") + 1] == "a model.gguf"
        assert args[args.index("--cache-ram") + 1] == "128"
        assert args[args.index("--parallel") + 1] == "1"
        assert "--no-context-shift" in args and "--no-op-offload" in args
        assert "--api-key" not in args and len(kw["env"]["LLAMA_API_KEY"]) == 64
    finally:
        model.close()


def test_start_wires_prompt_guard_from_the_gguf_vocab(server, monkeypatch, tmp_path):
    monkeypatch.setattr(ns, "free_port", lambda: 19999)
    monkeypatch.setattr(ns, "NativeProcess", lambda *a, **kw: Process())
    seen = []
    monkeypatch.setattr(ns.prompt_guard, "gguf_protected_tokens",
                        lambda path: seen.append(path) or ["<|im_end|>", "<|im_start|>"])
    model = ns.start("a model.gguf", root=tmp_path, n_ctx=4096, n_gpu_layers=0, n_threads=None, cache_mib=0)
    try:
        guard = model._guard()
        assert seen == ["a model.gguf"] and guard is not None
        clean, ticket = guard.sanitize({"messages": [{"role": "tool", "content": "x <|im_end|> y"}], "tools": None})
        assert ticket.active and "<|im_end|>" not in clean["messages"][0]["content"]
        ticket.close()
    finally:
        model.close()


def test_dead_engine_revokes_identity_and_tool_capabilities(server, tmp_path, monkeypatch):
    model, _, _, _ = server
    model.initialize(4096)
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"fixture")
    monkeypatch.setattr(ns, "bundle", lambda: (tmp_path, {"commit": "a" * 40}))
    monkeypatch.setattr(ns, "start", lambda *a, **kw: model)
    runtime = LLMRuntime()
    async def run():
        await runtime.load_model_async(str(model_path), backend="llama-server")
        assert runtime.current_model_sha256() is not None
        assert runtime.template_traits().supports_tools
        assert runtime.answers_verified() is False
        assert not lending_allowed(runtime)
        assert runtime.acceptance_summary()["status"] == "not_run"
        model.close()
        assert not runtime.is_loaded()
        assert runtime.current_model_sha256() is None
        assert runtime.template_traits() is None
        await runtime.unload_model_async()
    asyncio.run(run())


def test_changed_artifact_during_native_load_is_rejected(server, tmp_path, monkeypatch):
    model, _, _, _ = server
    path = tmp_path / "model.gguf"
    path.write_bytes(b"before")
    monkeypatch.setattr(ns, "bundle", lambda: (tmp_path, {"commit": "a" * 40}))
    def start(*args, **kwargs):
        path.write_bytes(b"after loading")
        return model
    monkeypatch.setattr(ns, "start", start)
    runtime = LLMRuntime()
    with pytest.raises(ModelIdentityError):
        asyncio.run(runtime.load_model_async(str(path), backend="llama-server"))
    assert model.closed and not runtime.is_loaded()


def test_missing_native_bundle_leaves_current_model_available(monkeypatch):
    runtime = LLMRuntime()
    current = Process()
    runtime._model = current
    def absent():
        raise ns.SplitError("missing", 503)
    monkeypatch.setattr(ns, "bundle", absent)
    with pytest.raises(ns.SplitError):
        asyncio.run(runtime.load_model_async("unused", backend="llama-server"))
    assert runtime._model is current and not current.closed
    runtime.unload_model()


@pytest.mark.parametrize("kwargs", [
    {"rpc_servers": ["100.64.0.2:50052"]}, {"native_cache_mib": -1},
    {"native_cache_mib": 4097}, {"n_ctx": 0}, {"n_gpu_layers": -2},
])
def test_invalid_native_configuration_is_rejected_before_loading(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(LLMRuntime().load_model_async("unused", backend="llama-server", **kwargs))


def test_default_backend_is_unchanged_and_cache_is_bounded():
    assert RuntimeSettings().backend == "llama-cpp-python"
    for value in (-1, 4097):
        with pytest.raises(ValidationError):
            RuntimeSettings(native_cache_mib=value)
    with pytest.raises(ValidationError):
        RuntimeSettings(backend="arbitrary-executable")


def test_native_benchmark_cannot_report_stream_chunks_as_tokens(server):
    model, _, _, _ = server
    runtime = LLMRuntime()
    runtime._model = model
    with pytest.raises(RuntimeError, match="does not support this benchmark"):
        asyncio.run(runtime.benchmark())


def test_native_answer_check_reports_backend_limitation(server):
    model, _, _, _ = server
    runtime = LLMRuntime()
    runtime._model = model
    with pytest.raises(RuntimeError, match="Answer checks are not available"):
        runtime._accept_loaded(None, None, "quick", False)


@pytest.mark.parametrize("cancel", [False, True])
def test_native_token_count_keeps_event_loop_live_and_drains_on_cancel(server, cancel):
    model, _, _, _ = server
    started, release = threading.Event(), threading.Event()
    def tokenize(*a, **kw):
        started.set()
        assert release.wait(2)
        return [1, 2, 3]
    model.tokenize = tokenize
    runtime = LLMRuntime()
    runtime._model = model
    async def run():
        task = asyncio.create_task(runtime.count_tokens("test"))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set() and not task.done()
            if cancel:
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done() and runtime._admission_lock.locked()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == 3
            assert not runtime._admission_lock.locked()
        finally:
            release.set()
    asyncio.run(run())


@pytest.mark.parametrize("stream,kind,category", [
    (False, "timeout", "timeout"), (True, "timeout", "timeout"),
    (False, "connection", "connection"), (True, "connection", "connection"),
    (False, "protocol", "transport"),
    (False, "http", "http"), (True, "http", "http"),
    (False, "format", "format"), (True, "format", "format"),
    (False, "unexpected", "unknown"),
])
def test_native_failure_category_is_safe_and_preserves_public_error(monkeypatch, stream, kind, category):
    secret = "private prompt and host: secret.example"
    client = httpx.Client
    def handle(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(secret, request=request)
        if kind == "connection":
            raise httpx.ConnectError(secret, request=request)
        if kind == "protocol":
            raise httpx.RemoteProtocolError(secret, request=request)
        if kind == "http":
            return httpx.Response(503, text=secret)
        if kind == "unexpected":
            raise RuntimeError(secret)
        return httpx.Response(200, content=(b"data: not-json\n\n" if stream else b"not-json"))
    monkeypatch.setattr(ns.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    model = ns.LocalServerModel(Process(), 19999, "key")
    try:
        with pytest.raises(ns.SplitError) as caught:
            result = model.create_chat_completion(messages=[{"role": "user", "content": secret}], stream=stream)
            if stream:
                list(result)
        error = caught.value
        assert error.failure_category == category
        assert error.code == "native_inference_failed" and error.status == 502
        assert str(error) == "The native local engine could not complete the request. Reload the model and retry."
        assert secret not in str(error) and secret not in error.failure_category
    finally:
        model.close()


def test_native_cancelled_has_fixed_category():
    error = ns.LocalServerModel._cancelled()
    assert (error.code, error.status, error.failure_category) == ("native_cancelled", 499, "cancelled")


@pytest.mark.parametrize("chunk", [
    {"choices": {}}, {"choices": [None]}, {"choices": [{"delta": "bad"}]},
    {"choices": [{"message": []}]}, {"choices": [{"delta": {"content": [1]}}]},
    {"choices": [{"message": {"content": 42}}]},
])
def test_native_stream_malformed_choices_are_format_errors(monkeypatch, chunk):
    client = httpx.Client
    data = ("data: " + json.dumps(chunk) + "\n\n").encode()
    monkeypatch.setattr(ns.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)), **kw))
    model = ns.LocalServerModel(Process(), 19999, "key")
    try:
        with pytest.raises(ns.SplitError) as caught:
            list(model.create_chat_completion(messages=[{"role": "user", "content": "hi"}], stream=True))
        assert caught.value.failure_category == "format"
    finally:
        model.close()


def test_native_stream_accepts_empty_usage_chunk(monkeypatch):
    client = httpx.Client
    data = b'data: {"choices": [], "usage": {"total_tokens": 1}}\n\ndata: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(ns.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)), **kw))
    model = ns.LocalServerModel(Process(), 19999, "key")
    try:
        assert len(list(model.create_chat_completion(messages=[{"role": "user", "content": "hi"}], stream=True))) == 2
    finally:
        model.close()


def test_split_error_category_rejects_untrusted_values():
    error = ns.SplitError("message", failure_category="private host or prompt")
    assert error.failure_category == "unknown"
    error.failure_category = {"private": "payload"}
    assert error.failure_category == "unknown"
    error.failure_category = None
    assert error.failure_category is None


def test_native_budget_uses_prepared_tokens_and_closes_ticket(server, monkeypatch):
    model, _, _, _ = server
    tickets = []
    class Ticket:
        closed = False
        def close(self): self.closed = True
    def prepare(self, request, body, *, force_completion=False):
        assert force_completion and '_potluck_input_token_limit' not in body
        ticket=Ticket(); tickets.append(ticket)
        return '/completion', {'prompt': [1, 2, 3, 4]}, ticket
    monkeypatch.setattr(ns.SplitModel, '_prepare', prepare)
    fmt={'type': 'json_object', 'schema': {'type': 'object'}}
    assert model.measure_investigation_prompt([{'role': 'user', 'content': 'hi'}], fmt) == 4
    assert tickets[-1].closed
    with pytest.raises(ns.SplitError, match='exceeds'):
        model._prepare(None, {'messages': [], 'response_format': fmt, '_potluck_investigation_completion': True,
                              '_potluck_input_token_limit': 3})
    assert tickets[-1].closed


def test_runtime_measurement_identity_context_and_drain(server, monkeypatch):
    model, _, _, _ = server
    profile=profiles.NativeInvestigationProfile('test-native', .7, .8, 20, 0, 'template',
        backend='llama-server', model_sha256='sha', engine_commit='commit', render_hashes=('off', 'on'))
    monkeypatch.setattr(profiles, 'resolve_profile', lambda name: profile)
    monkeypatch.setattr(profiles, 'validate_native', lambda *a, **kw: None)
    runtime=LLMRuntime(); runtime._model=model; runtime._model_id='model'; runtime._loaded_n_ctx=8192
    monkeypatch.setattr(runtime, 'current_model_sha256', lambda: 'sha')
    fmt={'type': 'json_object', 'schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}
    seen=[]
    model.measure_investigation_prompt=lambda messages, response_format: seen.append((messages, response_format)) or 400
    model.create_chat_completion=lambda **kw: {'choices': [{'finish_reason': 'stop'}]}
    args=dict(expected_model='model', expected_sha256='sha', generation_profile='test-native', response_format=fmt)
    async def run():
        measured=await runtime.measure_investigation_prompt([{'role': 'user', 'content': 'hi'}], **args)
        assert measured == {'input_tokens': 400, 'context_tokens': 8192} and len(seen)==1
        with pytest.raises(ModelIdentityError):
            await runtime.measure_investigation_prompt([], **{**args, 'expected_model': 'other'})
        with pytest.raises(ValueError, match='context changed'):
            await runtime.chat([], **args, expected_context_tokens=4096)
        entered=threading.Event(); release=threading.Event()
        def blocked(*a): entered.set(); release.wait(2); return 400
        model.measure_investigation_prompt=blocked
        task=asyncio.create_task(runtime.measure_investigation_prompt([], **args))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel(); await asyncio.sleep(.01)
        assert runtime._admission_lock.locked() and runtime._inference_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert not runtime._admission_lock.locked() and not runtime._inference_lock.locked()
    asyncio.run(run())
