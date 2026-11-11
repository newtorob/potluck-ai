"""Thinking settings survive API parsing, household retries and native rendering."""
import asyncio
import json

import httpx
import pytest

from potluck_local.services import gateway_routing as routing
from potluck_local.services import reasoning_controls as rc
from potluck_local.services.gateway_request import RequestError
from potluck_local.services.model_traits import TemplateTraits
from potluck_local.services.runtime import LLMRuntime
from test_gateway_reasoning_tools import (  # noqa: F401
    FakeRuntime, USER, client, serve, stream_text,
)
from test_peer_tools import peer_client
from test_split_native_guard import INJECTED, build


def native(monkeypatch, *, reply="42", always_thinks=False):
    def wrap(engine):
        def handle(request):
            response = engine(request)
            if request.url.path == "/apply-template":
                body = json.loads(request.content)
                thinking = (body.get("chat_template_kwargs", {}).get("enable_thinking", True)
                            and body.get("reasoning_effort") != "none")
                if thinking or always_thinks:
                    return httpx.Response(200, json={"prompt": response.json()["prompt"] + "<think>"})
            return response
        return handle
    model, engine, _ = build(monkeypatch, reply=reply, wrap=wrap)
    model.template_traits = TemplateTraits(opens_reasoning=True, thinks=True)
    runtime = LLMRuntime()
    runtime._model, runtime._model_id = model, "local-model"
    return runtime, engine


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("control", [
    {"chat_template_kwargs": {"enable_thinking": False}}, {"reasoning_effort": "none"},
])
def test_gateway_to_native_preserves_disabled_thinking_and_answer(client, monkeypatch, stream, guarded, control):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": INJECTED if guarded else "6 * 7?"}],
        "stream": stream, **control,
    })
    assert response.status_code == 200, response.text
    if stream:
        reasoning, answer, _, finish, _ = stream_text(response)
        assert (reasoning, answer, finish) == ("", "42", "stop")
    else:
        assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": "42"}
    rendered = engine.body("/apply-template" if guarded else "/v1/chat/completions")
    assert all(rendered[k] == v for k, v in control.items())
    if guarded:
        # Template arguments must not leak into the raw completion endpoint.
        assert not set(control) & engine.body("/v1/completions").keys()


@pytest.mark.parametrize("control", [
    {"chat_template_kwargs": {"enable_thinking": True}}, {"reasoning_effort": "high"},
])
@pytest.mark.parametrize("stream", [False, True])
def test_enabling_thinking_preserves_reasoning_and_answer(client, monkeypatch, control, stream):
    runtime, engine = native(monkeypatch, reply="Compute carefully.</think>42")
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={"messages": [USER], "stream": stream, **control})
    assert response.status_code == 200, response.text
    if stream:
        assert stream_text(response)[:2] == ("Compute carefully.", "42")
    else:
        message = response.json()["choices"][0]["message"]
        assert message["reasoning_content"] == "Compute carefully." and message["content"] == "42"
    assert all(engine.body("/v1/chat/completions")[k] == v for k, v in control.items())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_actual_render_overrides_defaults_and_a_model_ignoring_the_switch(client, monkeypatch, stream, guarded):
    runtime, _ = native(monkeypatch, reply="Compute carefully.</think>42", always_thinks=True)
    runtime._model.template_traits = TemplateTraits(opens_reasoning=False)
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": INJECTED if guarded else "6 * 7?"}],
        "stream": stream, "reasoning_effort": "none"})
    assert response.status_code == 200, response.text
    if stream:
        assert stream_text(response)[:2] == ("Compute carefully.", "42")
    else:
        message = response.json()["choices"][0]["message"]
        assert message["reasoning_content"] == "Compute carefully." and message["content"] == "42"


@pytest.mark.parametrize("body", [
    {"chat_template_kwargs": []}, {"chat_template_kwargs": {"enable_thinking": "false"}},
    {"chat_template_kwargs": {"enable_thinking": 0}}, {"chat_template_kwargs": {"enable_thinking": None}},
    {"chat_template_kwargs": {"messages": []}}, {"reasoning_effort": []}, {"reasoning_effort": "turbo"},
    {"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": True}},
    {"reasoning_effort": "high", "chat_template_kwargs": {"enable_thinking": False}},
])
def test_invalid_controls_fail_before_inference(client, monkeypatch, body):
    runtime = serve(monkeypatch, FakeRuntime())
    response = client.post("/v1/chat/completions", json={"messages": [USER], **body})
    assert response.status_code == 400, response.text
    assert not runtime.calls


@pytest.mark.parametrize("stream", [False, True])
def test_in_process_backend_refuses_instead_of_ignoring(client, monkeypatch, stream):
    runtime = serve(monkeypatch, FakeRuntime())
    response = client.post("/v1/chat/completions", json={
        "messages": [USER], "stream": stream, "reasoning_effort": "none"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reasoning_controls_unsupported"
    assert not runtime.calls


@pytest.mark.parametrize("stream", [False, True])
def test_responses_effort_reaches_native(client, monkeypatch, stream):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/responses", json={
        "input": "6 * 7?", "stream": stream, "reasoning": {"effort": "none"}})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["reasoning_effort"] == "none"
    if stream:
        assert '"delta": "42"' in response.text
    else:
        assert response.json()["output"][0]["content"][0]["text"] == "42"
        assert response.json()["reasoning"]["effort"] == "none"


def test_peer_advertises_and_forwards_only_on_native_backend(monkeypatch):
    runtime, engine = native(monkeypatch)
    client = peer_client(runtime, monkeypatch)
    assert client.get("/peer/capabilities").json()["reasoning_controls_version"] == 1
    response = client.post("/peer/chat/completions", json={
        "messages": [USER], "stream": True, "reasoning_effort": "none"})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["reasoning_effort"] == "none"
    runtime._model = object()
    assert client.get("/peer/capabilities").json()["reasoning_controls_version"] is None
    assert client.post("/peer/chat/completions", json={
        "messages": [USER], "reasoning_effort": "none"}).status_code == 400


def test_controls_are_rechecked_under_runtime_admission(monkeypatch):
    runtime, engine = native(monkeypatch)

    async def run(stream):
        async with runtime._inference_lock:
            assert runtime.supports_reasoning_controls()
            async def request():
                if stream:
                    return [chunk async for chunk in runtime.stream_chat_raw([USER], reasoning=rc.parse(effort="none"))]
                return await runtime.chat([USER], reasoning=rc.parse(effort="none"))
            task = asyncio.create_task(request())
            await asyncio.sleep(0)
            runtime._model = object()
        with pytest.raises(RequestError, match="native"):
            await task

    asyncio.run(run(False))
    runtime, engine = native(monkeypatch)
    asyncio.run(run(True))
    assert not engine.requests


def test_gateway_capabilities_report_native_transport_support(client, monkeypatch):
    runtime, _ = native(monkeypatch)
    serve(monkeypatch, runtime)
    assert client.get("/v1/potluck/capabilities").json()["reasoning_controls_version"] == 1
    runtime._model = object()
    assert client.get("/v1/potluck/capabilities").json()["reasoning_controls_version"] is None


@pytest.mark.parametrize("stream", [False, True])
def test_gateway_forwards_controls_to_household_route(client, monkeypatch, stream):
    serve(monkeypatch, FakeRuntime())
    peer = {"mesh_ip": "100.64.0.2", "_capabilities": {"reasoning_controls_version": 1}}
    async def find(model): return peer, None
    monkeypatch.setattr(routing, "find_peer", find)
    seen = []
    async def chunks(peer, messages, model, temperature, max_tokens, **kwargs):
        seen.append(kwargs["reasoning"].wire())
        yield {"id": "chatcmpl-test", "created": 1, "model": model,
               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
               "choices": [{"index": 0, "delta": {"content": "42"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(routing, "stream_peer_openai", chunks)
    response = client.post("/v1/chat/completions", json={
        "model": "remote", "messages": [USER], "stream": stream, "reasoning_effort": "none"})
    assert response.status_code == 200, response.text
    assert seen == [{"reasoning_effort": "none"}]
    assert response.headers["x-potluck-served-by"] == "household"
    assert (stream_text(response)[1] if stream else response.json()["choices"][0]["message"]["content"]) == "42"


def test_household_retry_preserves_controls_and_skips_old_engines(monkeypatch):
    peers = [{"mesh_ip": f"100.64.0.{i}", "node_name": f"n{i}"} for i in (2, 3, 4)]
    capabilities = {p["mesh_ip"]: {"available_for_inference": True, "loaded_model": "m",
                                    **({"reasoning_controls_version": 1} if i != 1 else {})}
                    for i, p in enumerate(peers)}
    async def household(): return peers
    async def caps(client, peer): return capabilities[peer["mesh_ip"]]
    monkeypatch.setattr(routing, "household_peers", household)
    monkeypatch.setattr(routing, "_peer_caps", caps)
    seen = []
    def handle(request):
        seen.append((request.url.host, json.loads(request.content)))
        if request.url.host == peers[0]["mesh_ip"]:
            return httpx.Response(503)
        return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"42"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    first = {**peers[0], "_capabilities": capabilities[peers[0]["mesh_ip"]]}
    answer = asyncio.run(routing.complete_peer_openai(first, [USER], "m", 0, 16, reasoning=rc.parse(effort="none")))
    assert answer["choices"][0]["message"]["content"] == "42"
    assert [host for host, _ in seen] == [peers[0]["mesh_ip"], peers[2]["mesh_ip"]]
    assert all(body["reasoning_effort"] == "none" for _, body in seen)


@pytest.mark.parametrize("route", ["old-peer", "pool"])
def test_unsupported_route_refuses_before_sending(client, monkeypatch, route):
    serve(monkeypatch, FakeRuntime())
    async def find(model):
        return ({"mesh_ip": "100.64.0.2", "_capabilities": {}} if route == "old-peer" else None), None
    monkeypatch.setattr(routing, "find_peer", find)
    monkeypatch.setattr("potluck_local.services.pool_routing.pool_enabled", lambda: True)
    response = client.post("/v1/chat/completions", json={
        "model": "remote", "messages": [USER], "reasoning_effort": "none"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reasoning_controls_unsupported"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
def test_budget_reaches_native_with_explicit_output_reserve(client, monkeypatch, stream, endpoint):
    runtime, engine = native(monkeypatch, reply="Consider.</think>42")
    serve(monkeypatch, runtime)
    body = ({"messages": [USER], "max_tokens": 128} if endpoint.endswith("completions")
            else {"input": "6 * 7?", "max_output_tokens": 128})
    response = client.post(endpoint, json={**body, "stream": stream, "reasoning_budget_tokens": 32})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["reasoning_budget_tokens"] == 32
    assert "42" in response.text


@pytest.mark.parametrize("budget", [-1, True, 1.5, "32", 128, 65536])
def test_invalid_budget_cannot_start_inference(client, monkeypatch, budget):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={
        "messages": [USER], "max_tokens": 128, "reasoning_budget_tokens": budget})
    assert response.status_code == 400, response.text
    assert not engine.requests


@pytest.mark.parametrize("max_tokens", [None, "128", True, 32])
def test_responses_budget_requires_larger_explicit_output_limit(client, monkeypatch, max_tokens):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/responses", json={
        "input": "hi", "reasoning_budget_tokens": 32, "max_output_tokens": max_tokens})
    assert response.status_code == 400, response.text
    assert not engine.requests


@pytest.mark.parametrize("stream", [False, True])
def test_budget_refuses_raw_guard_path_without_weakening_guard(client, monkeypatch, stream):
    runtime, engine = native(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": INJECTED}],
        "reasoning_budget_tokens": 32, "max_tokens": 128, "stream": stream})
    assert (response.status_code == 200 and '"error"' in response.text) if stream else response.status_code == 400
    assert "protected-token" in response.text
    assert not any(path in ("/v1/completions", "/v1/chat/completions") for path, _ in engine.requests)
    assert not runtime._model.closed
    retry = client.post("/v1/chat/completions", json={"messages": [USER], "max_tokens": 128, "reasoning_budget_tokens": 32})
    assert retry.status_code == 200


def test_budget_peer_capability_and_transport(monkeypatch):
    runtime, engine = native(monkeypatch)
    client = peer_client(runtime, monkeypatch)
    assert client.get("/peer/capabilities").json()["reasoning_budget_version"] == 1
    response = client.post("/peer/chat/completions", json={
        "messages": [USER], "reasoning_budget_tokens": 0, "max_tokens": 128})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["reasoning_budget_tokens"] == 0


def test_budget_cannot_route_to_a_peer_with_only_effort_support(client, monkeypatch):
    serve(monkeypatch, FakeRuntime())
    async def find(model): return {"mesh_ip": "100.64.0.2", "_capabilities": {"reasoning_controls_version": 1}}, None
    monkeypatch.setattr(routing, "find_peer", find)
    response = client.post("/v1/chat/completions", json={
        "model": "remote", "messages": [USER], "max_tokens": 128, "reasoning_budget_tokens": 32})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reasoning_controls_unsupported"


def test_budget_retry_filters_old_peers_and_preserves_exact_budget(monkeypatch):
    peers = [{"mesh_ip": f"100.64.0.{i}", "node_name": f"n{i}"} for i in (2, 3, 4)]
    caps = {p["mesh_ip"]: {"available_for_inference": True, "loaded_model": "m", "reasoning_controls_version": 1,
                           **({"reasoning_budget_version": 1} if i != 1 else {})} for i, p in enumerate(peers)}
    async def household(): return peers
    async def capabilities(client, peer): return caps[peer["mesh_ip"]]
    monkeypatch.setattr(routing, "household_peers", household)
    monkeypatch.setattr(routing, "_peer_caps", capabilities)
    seen = []
    def handle(request):
        seen.append((request.url.host, json.loads(request.content)))
        if request.url.host == peers[0]["mesh_ip"]: return httpx.Response(503)
        return httpx.Response(200, text='data: {"choices":[{"index":0,"delta":{"content":"42"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    first = {**peers[0], "_capabilities": caps[peers[0]["mesh_ip"]]}
    answer = asyncio.run(routing.complete_peer_openai(first, [USER], "m", 0, 128, reasoning=rc.parse(budget=32, max_tokens=128)))
    assert answer["choices"][0]["message"]["content"] == "42"
    assert [host for host, _ in seen] == [peers[0]["mesh_ip"], peers[2]["mesh_ip"]]
    assert all(body["reasoning_budget_tokens"] == 32 for _, body in seen)
