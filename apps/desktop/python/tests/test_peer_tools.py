"""Tool calls between household machines.

The serving machine advertises `supports_tools` on /peer/capabilities and takes
`tools` / `tool_choice` on /peer/chat/completions; the requesting machine reads
the calls out of the streamed text. A request without tools is served exactly as
before. What reaches the requester when it asks for something this node refuses
is fixed text.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers import peer
from potluck_local.services import gateway_routing as direct
from potluck_local.services.runtime import LLMRuntime

QWEN_TOOLS = """{%- if tools %}{{- '<|im_start|>system\\n' }}{%- for tool in tools %}{{- tool | tojson }}{%- endfor %}{%- endif %}
{%- for message in messages %}{{- '<|im_start|>' + message.role + '\\n' + message.content + '<|im_end|>\\n' }}{%- endfor %}
{%- if add_generation_prompt %}{{- '<|im_start|>assistant\\n' }}{%- endif %}"""
NO_TOOLS = "{%- for m in messages %}{{ m.role }}: {{ m.content }}\n{%- endfor %}{%- if add_generation_prompt %}assistant: {%- endif %}"

WEATHER = {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}
USER = {"role": "user", "content": "weather in Paris?"}
FIXED_REFUSAL = "This machine's model does not support tool calls."


class Model:
    def __init__(self, template: str | None, pieces=("<tool_call>", '{"name": "get_weather", "arguments": {}}', "</tool_call>")):
        self.metadata = {"tokenizer.chat_template": template} if template else {}
        self.pieces = list(pieces)
        self.kwargs: list[dict] = []

    def create_chat_completion(self, **kw):
        self.kwargs.append(kw)
        if kw.get("stream"):
            return iter([{"choices": [{"delta": {"content": p}}]} for p in self.pieces])
        return {"choices": [{"message": {"content": "".join(self.pieces)}, "finish_reason": "stop"}], "usage": {}}


def runtime_with(model: Model) -> LLMRuntime:
    rt = LLMRuntime()
    rt._model, rt._model_id = model, "test"
    rt._load_report = {"offloaded_layers": 0, "used_n_gpu_layers": 0}
    return rt


def peer_client(rt, monkeypatch) -> TestClient:
    monkeypatch.setattr(peer, "get_runtime", lambda: rt)
    monkeypatch.setattr(peer, "get_settings", lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))

    async def hardware():
        return {}

    async def record(*args, **kwargs):
        pass

    monkeypatch.setattr(peer, "_cached_hardware", hardware)
    monkeypatch.setattr(peer, "_record_served", record)
    monkeypatch.setattr("potluck_local.services.link_detect.detect_link_type", lambda: "wired")
    app = FastAPI()
    app.include_router(peer.router, prefix="/peer")
    return TestClient(app)


# ---- the capability flag ---------------------------------------------------------


@pytest.mark.parametrize("template,expected", [(QWEN_TOOLS, True), (NO_TOOLS, False), (None, False)])
def test_capabilities_say_whether_the_loaded_model_supports_tools(monkeypatch, template, expected):
    body = peer_client(runtime_with(Model(template)), monkeypatch).get("/peer/capabilities").json()
    assert body["supports_tools"] is expected


def test_capabilities_say_false_when_nothing_is_loaded(monkeypatch):
    body = peer_client(LLMRuntime(), monkeypatch).get("/peer/capabilities").json()
    assert body["supports_tools"] is False and body["loaded_model"] is None


def test_capabilities_keep_every_field_they_had():
    fields = set(peer.PeerCapabilities.model_fields)
    assert {"hostname", "loaded_model", "contributor_mode", "available_for_inference", "protocol_version",
            "answers_verified", "available_for_pipeline", "link_type", "supports_tools"} <= fields
    assert peer.PeerCapabilities.model_fields["supports_tools"].default is False


# ---- serving a tools request ------------------------------------------------------


@pytest.mark.parametrize("stream", [True, False])
def test_a_tools_request_reaches_the_model_and_the_text_streams_back(monkeypatch, stream):
    model = Model(QWEN_TOOLS)
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "stream": stream, "tools": [WEATHER]})
    assert resp.status_code == 200
    assert model.kwargs[0]["tools"] == [WEATHER] and "response_format" not in model.kwargs[0]
    assert "<tool_call>" in resp.text and "get_weather" in resp.text


def test_a_forced_choice_adds_the_grammar_on_the_serving_machine(monkeypatch):
    model = Model(QWEN_TOOLS)
    peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "tools": [WEATHER], "tool_choice": "required"})
    schema = model.kwargs[0]["response_format"]["schema"]
    assert schema["properties"]["name"] == {"const": "get_weather"} and "tool_choice" not in model.kwargs[0]


def test_tool_history_is_accepted_and_arguments_reach_the_template_as_an_object(monkeypatch):
    model = Model(QWEN_TOOLS)
    history = [USER,
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "call12345", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]},
               {"role": "tool", "content": '{"temp_c": 17}', "tool_call_id": "call12345"}]
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": history, "model": "test", "tools": [WEATHER]})
    assert resp.status_code == 200
    sent = model.kwargs[0]["messages"]
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}
    assert sent[2] == {"role": "tool", "content": '{"temp_c": 17}', "tool_call_id": "call12345"}
    assert sent[0] == USER  # an ordinary message has no empty tool fields


def test_a_request_without_tools_is_served_exactly_as_before(monkeypatch):
    model = Model(QWEN_TOOLS)
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "stream": True})
    assert resp.status_code == 200
    assert set(model.kwargs[0]) == {"messages", "temperature", "max_tokens", "stop", "stream"}
    assert model.kwargs[0]["messages"] == [USER]


# ---- refusing what this node cannot do ---------------------------------------------


@pytest.mark.parametrize("template", [NO_TOOLS, None])
def test_a_node_whose_model_has_no_tool_section_refuses_with_fixed_text(monkeypatch, template):
    model = Model(template)
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "tools": [WEATHER]})
    assert resp.status_code == 400 and resp.json()["detail"] == FIXED_REFUSAL
    assert model.kwargs == []


def test_tool_history_without_tools_is_refused_by_a_node_without_support(monkeypatch):
    model = Model(NO_TOOLS)
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER, {"role": "tool", "content": "{}"}], "model": "test"})
    assert resp.status_code == 400 and model.kwargs == []


def test_an_invalid_tools_request_gets_fixed_text_that_does_not_repeat_the_request(monkeypatch):
    model = Model(QWEN_TOOLS)
    resp = peer_client(runtime_with(model), monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "tools": [{"type": "function", "function": {"name": "secret name with spaces"}}]})
    assert resp.status_code == 400 and resp.json()["detail"] == "The tool request was not valid."
    assert "secret" not in resp.text and model.kwargs == []


def test_a_batch_request_cannot_carry_tools(monkeypatch):
    model = Model(QWEN_TOOLS)
    rt = runtime_with(model)
    rt.assert_model_identity = lambda *a, **k: None  # the pin is not what this test is about
    resp = peer_client(rt, monkeypatch).post("/peer/chat/completions", json={
        "messages": [USER], "model": "test", "stream": True, "potluck_batch": True,
        "potluck_model_sha256": "a" * 64, "tools": [WEATHER]})
    assert resp.status_code == 400 and model.kwargs == []


# ---- the requesting side -----------------------------------------------------------


@pytest.fixture
def capture(monkeypatch):
    seen: list[dict] = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = 'data: {"choices": [{"delta": {"content": "hi"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def test_the_peer_request_carries_tools_only_when_there_are_some(capture):
    peer_info = {"mesh_ip": "100.64.0.2"}
    messages = [USER]
    asyncio.run(direct.complete_peer_openai(peer_info, messages, "test", 0.7, 20))
    asyncio.run(direct.complete_peer_openai(peer_info, messages, "test", 0.7, 20, tools=[WEATHER], tool_choice="auto"))
    assert "tools" not in capture[0] and "tool_choice" not in capture[0]
    assert capture[1]["tools"] == [WEATHER] and capture[1]["tool_choice"] == "auto"


def test_a_retry_only_considers_machines_that_advertise_tools(monkeypatch):
    async def peers():
        return [{"node_name": "a", "mesh_ip": "100.64.0.2"}, {"node_name": "b", "mesh_ip": "100.64.0.3"},
                {"node_name": "c", "mesh_ip": "100.64.0.4"}]

    caps = {
        "100.64.0.2": {"available_for_inference": True, "loaded_model": "m"},
        "100.64.0.3": {"available_for_inference": True, "loaded_model": "m", "supports_tools": False},
        "100.64.0.4": {"available_for_inference": True, "loaded_model": "m", "supports_tools": True},
    }

    async def peer_caps(client, p):
        return caps[p["mesh_ip"]]

    monkeypatch.setattr(direct, "household_peers", peers)
    monkeypatch.setattr(direct, "_peer_caps", peer_caps)
    assert asyncio.run(direct._replacement_peer("m", set()))["mesh_ip"] == "100.64.0.2"
    assert asyncio.run(direct._replacement_peer("m", set(), tools=True))["mesh_ip"] == "100.64.0.4"
    assert asyncio.run(direct._replacement_peer("m", {"100.64.0.4"}, tools=True)) is None


def test_a_proof_retry_only_considers_machines_that_prove_the_same_markers(monkeypatch):
    async def peers():
        return [{"node_name": n, "mesh_ip": ip} for n, ip in (("a", "100.64.0.2"), ("b", "100.64.0.3"), ("c", "100.64.0.4"))]

    tools = {"available_for_inference": True, "loaded_model": "m", "supports_tools": True}
    caps = {
        "100.64.0.2": tools,  # an older release lists no markers
        "100.64.0.3": {**tools, "tool_markers": ["<tool_call>", "[TOOL_CALLS]"]},
        "100.64.0.4": {**tools, "tool_markers": ["<tool_call>"]},
    }

    async def peer_caps(client, p):
        return caps[p["mesh_ip"]]

    monkeypatch.setattr(direct, "household_peers", peers)
    monkeypatch.setattr(direct, "_peer_caps", peer_caps)
    proven = frozenset({"<tool_call>"})
    assert asyncio.run(direct._replacement_peer("m", set(), tools=True))["mesh_ip"] == "100.64.0.2"
    assert asyncio.run(direct._replacement_peer("m", set(), tools=True, markers=proven))["mesh_ip"] == "100.64.0.4"
    assert asyncio.run(direct._replacement_peer("m", {"100.64.0.4"}, tools=True, markers=proven)) is None


def test_a_proof_stream_retries_with_the_first_machines_markers(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw))
    asked: list[dict] = []

    async def replacement(model, excluded, **kwargs):
        asked.append(kwargs)
        return None

    monkeypatch.setattr(direct, "_replacement_peer", replacement)
    first = {"mesh_ip": "100.64.0.2", "_capabilities": {"supports_tools": True, "tool_markers": ["<tool_call>"]}}
    for proof in (True, False):
        with pytest.raises(direct.RemoteStreamError):
            asyncio.run(direct.complete_peer_openai(first, [USER], "test", 0.7, 20, tools=[WEATHER], tool_choice="auto",
                                                    **({"tool_proof": True} if proof else {})))
    assert asked == [{"tools": True, "markers": frozenset({"<tool_call>"})}, {"tools": True}]


# ---- proof that a call marker was sampled as the marker token ------------------------------------


class MarkerModel(Model):
    """A model whose vocab has Qwen's <tool_call> as one token (see services/tool_markers.py)."""

    def __init__(self, template):
        super().__init__(template)
        self._potluck_tool_markers = {7: "<tool_call>"}

    def generate(self, tokens, **kw):
        yield from ()


def test_capabilities_list_the_markers_the_model_can_prove(monkeypatch):
    body = peer_client(runtime_with(MarkerModel(QWEN_TOOLS)), monkeypatch).get("/peer/capabilities").json()
    assert body["tool_markers"] == ["<tool_call>"]
    body = peer_client(runtime_with(Model(QWEN_TOOLS)), monkeypatch).get("/peer/capabilities").json()
    assert body["tool_markers"] == []
    body = peer_client(LLMRuntime(), monkeypatch).get("/peer/capabilities").json()
    assert body["tool_markers"] == []
    assert peer.PeerCapabilities.model_fields["tool_markers"].default == []


def test_a_proof_request_reaches_the_runtime_only_when_the_requester_asked(monkeypatch):
    rt = runtime_with(MarkerModel(QWEN_TOOLS))
    seen = []
    real = rt.stream_chat_raw

    def spy(*args, **kwargs):
        seen.append(kwargs.get("tool_proof"))
        return real(*args, **kwargs)

    rt.stream_chat_raw = spy
    client = peer_client(rt, monkeypatch)
    for proof in (False, True):
        resp = client.post("/peer/chat/completions", json={
            "messages": [USER], "model": "test", "stream": True, "tools": [WEATHER], "potluck_tool_proof": proof})
        assert resp.status_code == 200
    assert seen == [None, True]


def test_the_peer_request_asks_for_proof_only_with_tools(capture):
    peer_info = {"mesh_ip": "100.64.0.2"}
    asyncio.run(direct.complete_peer_openai(peer_info, [USER], "test", 0.7, 20, tools=[WEATHER], tool_choice="auto",
                                            tool_proof=True))
    asyncio.run(direct.complete_peer_openai(peer_info, [USER], "test", 0.7, 20, tools=[WEATHER], tool_choice="auto"))
    asyncio.run(direct.complete_peer_openai(peer_info, [USER], "test", 0.7, 20, tool_proof=True))
    assert capture[0]["potluck_tool_proof"] is True
    assert "potluck_tool_proof" not in capture[1] and "potluck_tool_proof" not in capture[2]


def test_the_gateway_reads_proven_markers_from_the_engine_or_the_peer_capabilities():
    from potluck_local.routers import gateway

    local = SimpleNamespace(proves_tool_markers=lambda: frozenset({"<tool_call>"}))
    assert gateway._proven_markers(local, None) == {"<tool_call>"}
    assert gateway._proven_markers(SimpleNamespace(), None) == frozenset()
    assert gateway._proven_markers(local, {"_capabilities": {"tool_markers": ["[TOOL_CALLS]"]}}) == {"[TOOL_CALLS]"}
    assert gateway._proven_markers(local, {"_capabilities": {}}) == frozenset()  # an older machine proves nothing
    assert gateway._proven_markers(local, {"_capabilities": {"tool_markers": "<tool_call>"}}) == frozenset()
    broken = SimpleNamespace(proves_tool_markers=lambda: 1 / 0)
    assert gateway._proven_markers(broken, None) == frozenset()


def test_the_plan_asks_the_engine_for_proof_only_when_markers_are_proven():
    from potluck_local.routers import gateway
    from potluck_local.services import gateway_reply, gateway_request

    prepared = gateway_request.prepare([USER], [WEATHER], None)

    def plan(proven):
        options = gateway_reply.ShapeOptions(offered=prepared.offered_names, proven=frozenset(proven))
        return SimpleNamespace(job=SimpleNamespace(prepared=prepared), options=options)

    assert "tool_proof" not in gateway.ChatPlan.tool_kwargs.fget(plan(set()))
    assert gateway.ChatPlan.tool_kwargs.fget(plan({"<tool_call>"}))["tool_proof"] is True
    bare = gateway_request.prepare([USER], None, None)
    plain = SimpleNamespace(job=SimpleNamespace(prepared=bare), options=gateway_reply.ShapeOptions(proven=frozenset({"x"})))
    assert gateway.ChatPlan.tool_kwargs.fget(plain) == {}
