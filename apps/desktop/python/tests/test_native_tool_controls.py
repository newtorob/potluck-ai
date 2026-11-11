"""The one-call contract reaches the native engine; old routes cannot drop it."""
import asyncio
import json
import httpx
import pytest
from potluck_local.services import gateway_routing as routing
from potluck_local.services.model_traits import TemplateTraits
from test_reasoning_controls import native
from test_gateway_reasoning_tools import FakeRuntime, USER, client, serve  # noqa: F401
from test_peer_tools import peer_client
from test_split_native_guard import TOOLS


def tool_runtime(monkeypatch):
    runtime, engine = native(monkeypatch)
    runtime._model.template_traits = TemplateTraits(supports_tools=True, thinks=True, opens_reasoning=True)
    return runtime, engine


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
def test_gateway_preserves_concurrency_to_native(client, monkeypatch, stream, parallel):
    runtime, engine = tool_runtime(monkeypatch)
    serve(monkeypatch, runtime)
    response = client.post("/v1/chat/completions", json={
        "messages": [USER], "tools": TOOLS, "stream": stream, "parallel_tool_calls": parallel})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["parallel_tool_calls"] is parallel
    caps = client.get("/v1/potluck/capabilities").json()
    assert caps["parallel_tool_calls_request_version"] == caps["parallel_tool_calls_version"] == 1


def test_peer_preserves_concurrency_and_advertises_it(monkeypatch):
    runtime, engine = tool_runtime(monkeypatch)
    client = peer_client(runtime, monkeypatch)
    assert client.get("/peer/capabilities").json()["parallel_tool_calls_version"] == 1
    response = client.post("/peer/chat/completions", json={
        "messages": [USER], "tools": TOOLS, "stream": True, "parallel_tool_calls": False})
    assert response.status_code == 200, response.text
    assert engine.body("/v1/chat/completions")["parallel_tool_calls"] is False


@pytest.mark.parametrize("value", [0, "false", [], {}])
def test_invalid_concurrency_is_not_silently_coerced(client, monkeypatch, value):
    runtime, engine = tool_runtime(monkeypatch)
    serve(monkeypatch, runtime)
    assert client.post("/v1/chat/completions", json={"messages": [USER], "tools": TOOLS, "parallel_tool_calls": value}).status_code == 422
    assert not engine.requests


def test_old_household_route_refuses_before_inference(client, monkeypatch):
    serve(monkeypatch, FakeRuntime())
    async def find(model): return {"mesh_ip": "100.64.0.2", "_capabilities": {"supports_tools": True}}, None
    monkeypatch.setattr(routing, "find_peer", find)
    response = client.post("/v1/chat/completions", json={"model": "remote", "messages": [USER], "tools": TOOLS, "parallel_tool_calls": False})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "tool_concurrency_unsupported"


def test_retry_keeps_false_and_skips_peers_without_concurrency_support(monkeypatch):
    peers = [{"mesh_ip": f"100.64.0.{i}", "node_name": f"n{i}"} for i in (2, 3, 4)]
    caps = {p["mesh_ip"]: {"available_for_inference": True, "loaded_model": "m", "supports_tools": True,
                           **({"parallel_tool_calls_version": 1} if i != 1 else {})} for i, p in enumerate(peers)}
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
    answer = asyncio.run(routing.complete_peer_openai(first, [USER], "m", 0, 128, tools=TOOLS, parallel_tool_calls=False))
    assert answer["choices"][0]["message"]["content"] == "42"
    assert [host for host, _ in seen] == [peers[0]["mesh_ip"], peers[2]["mesh_ip"]]
    assert all(body["parallel_tool_calls"] is False for _, body in seen)
