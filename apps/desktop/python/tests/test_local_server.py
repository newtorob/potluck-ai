"""Native local backend contracts, without downloading weights or starting inference."""
import asyncio
import json
import threading

import httpx
import pytest
from pydantic import ValidationError

from potluck_local.services import local_server as ns
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
    client = httpx.Client

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
