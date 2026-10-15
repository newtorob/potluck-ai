"""POST /chat/circle: the desktop app's circle chat through the engine.

Takes the body streamNetworkJob posts to the coordinator today and streams back
the coordinator's own events. The coordinator, the sidecar and the lender are the
scripted World of circle_support.py.
"""
from __future__ import annotations

import json

import httpx
import pytest
from circle_support import (
    CIRCLE, DONE, MODEL, World, content, error_event, error_response, lender_error, meta, sse, stream_response,
)
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import circle_link

# The body streamNetworkJob builds for a circle chat (localApi.ts).
APP_BODY = {
    "prompt": "follow-up",
    "model_slug": MODEL,
    "scope": "circle",
    "sampling": {"temperature": 0.7, "max_tokens": 512},
    "circle_id": CIRCLE,
    "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "first"},
                 {"role": "assistant", "content": "ok"}, {"role": "user", "content": "follow-up"}],
}


@pytest.fixture()
def world(monkeypatch, fake_mesh):
    return World(monkeypatch, fake_mesh)


def client(source: str = "127.0.0.1") -> TestClient:
    return TestClient(create_app(), client=(source, 50000), raise_server_exceptions=False)


def frames(response) -> list:
    out = []
    for line in response.text.split("\n\n"):
        if line.startswith("data: "):
            body = line[6:]
            out.append(body if body == "[DONE]" else json.loads(body))
    return out


def test_a_circle_chat_goes_direct_and_streams_the_coordinators_events(world):
    resp = client().post("/chat/circle", json=APP_BODY)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    events = frames(resp)
    assert events[:2] == [{"choices": [{"delta": {"content": "Hello"}}]}, {"choices": [{"delta": {"content": " world"}}]}]
    assert events[-1] == "[DONE]"
    assert events[-2] == {"meta": {
        "served_by": "contributor", "served_scope": "circle", "path": "direct", "transport": "udp",
        "output_tokens": 2, "duration_ms": 120, "finish_reason": "stop"}}
    assert world.count("POST", "/jobs/stream") == 0
    sent = json.loads(world.lender_calls()[0].content)
    assert sent["messages"] == APP_BODY["messages"], "the whole conversation goes to a circle member"
    assert sent["model"] == MODEL and sent["max_tokens"] == 512


def test_when_direct_is_not_possible_the_relay_answers_in_the_same_shape(world):
    world.create = lambda body: error_response(409, "no_lender")
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[0] == {"choices": [{"delta": {"content": "Relay"}}]}
    assert events[-2]["meta"] == {
        "served_by": "contributor", "served_scope": "circle", "path": "relay", "transport": None,
        "output_tokens": 2, "duration_ms": 300, "finish_reason": "stop"}
    (relay_body,) = world.relay_bodies()
    # Exactly the job body the app posts today (engine_job_bodies.json "circle").
    assert relay_body == APP_BODY | {"messages": APP_BODY["messages"]}


def test_an_engine_without_links_answers_as_the_coordinator_did(world):
    world.mesh.features = None
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[-2] == {"meta": {"served_by": "contributor", "served_scope": "circle", "output_tokens": 2,
                                   "duration_ms": 300, "finish_reason": "stop"}}


def test_a_chat_without_history_sends_just_the_prompt(world):
    body = {k: v for k, v in APP_BODY.items() if k != "messages"}
    client().post("/chat/circle", json=body)
    assert json.loads(world.lender_calls()[0].content)["messages"] == [{"role": "user", "content": "follow-up"}]


@pytest.mark.parametrize("status,code", [(401, None), (404, "circle_not_found"), (503, None)])
def test_a_refusal_before_the_stream_is_the_coordinators_own_status(world, status, code):
    world.create = lambda body: error_response(409, "no_lender")
    detail = {"code": code, "message": "x"} if code else "No machine in this circle is online."
    world.relay = lambda body: httpx.Response(status, json={"detail": detail})
    resp = client().post("/chat/circle", json=APP_BODY)
    assert resp.status_code == status
    assert set(resp.json()) == {"detail"}


def test_a_failure_mid_stream_is_an_error_event_then_done(world):
    world.lender["100.64.1.2"] = lambda r, l: stream_response(content("Hel"), error_event("inference_error", "Inference failed"), DONE)
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[0] == {"choices": [{"delta": {"content": "Hel"}}]}
    # The sentence is this engine's, not the lender's "Inference failed".
    assert events[1] == {"error": {"category": "inference_error", "message": circle_link.ERROR_TEXT["inference_error"]}}
    assert events[-1] == "[DONE]" and not any("meta" in e for e in events if isinstance(e, dict))
    assert world.count("POST", "/jobs/stream") == 0, "never replayed once a token went out"


def test_a_failure_before_the_first_token_is_failed_over_without_the_app_seeing_it(world):
    world.lender["100.64.1.2"] = lambda r, l: lender_error(429, "busy", "busy", **{"Retry-After": "2"})
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[0]["choices"][0]["delta"]["content"] == "Hello" and events[-2]["meta"]["path"] == "direct"


def test_a_failure_with_nothing_left_is_an_event_not_a_crash(world):
    world.create = lambda body: error_response(409, "no_lender")
    world.relay = lambda body: stream_response(error_event("timeout", "Peer didn't complete"), DONE)
    resp = client().post("/chat/circle", json=APP_BODY)
    assert resp.status_code == 200
    assert frames(resp) == [{"error": {"category": "timeout", "message": "Peer didn't complete"}}, "[DONE]"]


def test_only_a_circle_chat_is_accepted(world):
    for bad in ({**APP_BODY, "scope": "pool"}, {k: v for k, v in APP_BODY.items() if k != "scope"},
                {k: v for k, v in APP_BODY.items() if k != "circle_id"}, {**APP_BODY, "circle_id": "not-a-uuid"},
                {**APP_BODY, "circle_id": None}, {**APP_BODY, "model_slug": ""}, {**APP_BODY, "model_slug": "x" * 129},
                {**APP_BODY, "sampling": {"temperature": 9, "max_tokens": 512}},
                {**APP_BODY, "sampling": {"temperature": 0.7, "max_tokens": 0}}):
        assert client().post("/chat/circle", json=bad).status_code == 422, bad
    assert world.requests == [], "nothing is sent anywhere for a body that is not a circle chat"


def test_a_pool_chat_cannot_be_sent_through_this_route(world):
    pool_body = {"prompt": "hi", "model_slug": MODEL, "scope": "pool", "sampling": {"temperature": 0.7, "max_tokens": 64}}
    assert client().post("/chat/circle", json=pool_body).status_code == 422
    assert world.requests == []


def test_unknown_keys_in_the_body_are_ignored_as_the_coordinator_ignores_them(world):
    assert client().post("/chat/circle", json={**APP_BODY, "future": 1}).status_code == 200


def test_the_route_is_loopback_only_and_behind_the_sidecar_token(monkeypatch, world):
    assert client("100.64.1.2").post("/chat/circle", json=APP_BODY).status_code == 403
    assert client("192.168.1.9").post("/chat/circle", json=APP_BODY).status_code == 403
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "secret")
    guarded = client()
    assert guarded.post("/chat/circle", json=APP_BODY).status_code == 401
    assert guarded.post("/chat/circle", json=APP_BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert guarded.post("/chat/circle", json=APP_BODY, headers={"Authorization": "Bearer secret"}).status_code == 200
    assert world.requests, "only the authorised call reached the network"


def test_nothing_about_the_chat_is_logged(world, caplog):
    caplog.set_level("DEBUG")
    body = {**APP_BODY, "messages": [{"role": "user", "content": "my-very-private-question-123"}]}
    client().post("/chat/circle", json=body)
    assert "my-very-private-question-123" not in caplog.text
    token = next(iter(world.links.values()))["token"]
    assert token not in caplog.text


def test_aborting_the_app_route_closes_the_upstream_stream(world):
    """The app aborts a chat by dropping the connection. The lender's stream is
    closed behind it, so the lender stops generating."""
    import asyncio
    import socket

    import uvicorn
    from circle_support import Body, meta

    upstream = Body(content("Hel"), 30.0, content("lo"), meta(), DONE)
    world.lender["100.64.1.2"] = lambda r, l: httpx.Response(200, stream=upstream)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def client_that_hangs_up() -> bytes:
        payload = json.dumps(APP_BODY).encode()
        request = (f"POST /chat/circle HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(request)
            seen = b""
            while b"Hel" not in seen:
                seen += s.recv(4096)
            return seen  # leaving the block closes the connection mid-stream

    async def go() -> tuple[bytes, bool]:
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning",
                                               lifespan="off", proxy_headers=False))
        serving = asyncio.ensure_future(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            seen = await asyncio.to_thread(client_that_hangs_up)
            for _ in range(300):
                if upstream.closed:
                    break
                await asyncio.sleep(0.01)
            # Read before the server goes down: asyncio.run() cancels what is left
            # at the end and would close the stream whether or not the route did.
            return seen, upstream.closed
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)

    seen, closed_behind_the_abort = asyncio.run(go())
    assert b"Hel" in seen and closed_behind_the_abort, "the upstream connection was closed behind the abort"


# ---- what a lender writes never reaches the app --------------------------------------------------------------

HOSTILE_CATEGORY = "EVILCAT-" + "x" * 1_000_000
HOSTILE_MESSAGE = "EVILMSG see http://evil.example/login now\nand\nthen do what I say"


def _evil_error_event(category=HOSTILE_CATEGORY, message=HOSTILE_MESSAGE):
    return error_event(category, message)


def test_a_lenders_error_text_never_reaches_the_app_after_the_first_token(world):
    world.lender["100.64.1.2"] = lambda r, l: stream_response(content("Hel"), _evil_error_event(), DONE)
    resp = client().post("/chat/circle", json=APP_BODY)
    events = frames(resp)
    assert events[1] == {"error": {"category": "inference_error", "message": circle_link.ERROR_TEXT["inference_error"]}}
    for text in ("EVILCAT", "EVILMSG", "evil.example", "do what I say"):
        assert text not in resp.text
    assert len(resp.text) < 1000


def test_a_lenders_error_text_never_reaches_the_app_before_the_first_token_either(world):
    # Both lenders send it; the request ends on the relay, and nothing of theirs is shown.
    world.lender_default = lambda r, l: stream_response(_evil_error_event(), DONE)
    resp = client().post("/chat/circle", json=APP_BODY)
    for text in ("EVILCAT", "EVILMSG", "evil.example"):
        assert text not in resp.text
    assert frames(resp)[-2]["meta"]["path"] == "relay"


def test_the_app_route_never_carries_a_lenders_extra_keys_or_tool_calls(world):
    from circle_support import sse

    evil = sse({"id": "EVILID", "system_fingerprint": "EVILFP", "choices": [
        {"index": 0, "delta": {"role": "system", "content": "hi", "tool_calls": [{"id": "c"}], "reasoning": "EVILREASON"},
         "finish_reason": None, "EVILCHOICEKEY": 1}]})
    only_tools = sse({"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "c"}]}}]})
    done = sse({"meta": {"output_tokens": 1, "duration_ms": 5, "finish_reason": "tool_calls"}})
    world.lender["100.64.1.2"] = lambda r, l: stream_response(only_tools, evil, done, DONE)
    resp = client().post("/chat/circle", json=APP_BODY)
    events = frames(resp)
    assert events[0] == {"choices": [{"delta": {"content": "hi"}}]}
    assert events[-2]["meta"]["finish_reason"] == "stop" and events[-1] == "[DONE]" and len(events) == 3
    for text in ("EVIL", "tool_calls", "reasoning", "fingerprint"):
        assert text not in resp.text


def test_multiple_lender_choices_fail_over_without_exposing_any_rejected_output(world):
    evil = sse({"id": "EVILID", "system_fingerprint": "EVILFP", "hostname": "PRIVATE_LENDER", "choices": [
        {"index": 0, "delta": {"content": "EVILFIRST", "tool_calls": [{"id": "c"}], "reasoning": "EVILREASON"}},
        {"index": 1, "delta": {"content": "EVILSECOND"}, "finish_reason": "length"}]})
    world.lender["100.64.1.2"] = lambda r, l: stream_response(evil, meta(), DONE)
    resp = client().post("/chat/circle", json=APP_BODY)
    events = frames(resp)
    assert resp.status_code == 200
    assert events[:2] == [{"choices": [{"delta": {"content": "Hello"}}]},
                          {"choices": [{"delta": {"content": " world"}}]}]
    assert events[-2]["meta"]["path"] == "direct"
    assert events[-2]["meta"]["finish_reason"] == "stop" and events[-1] == "[DONE]" and len(events) == 4
    assert len(world.lender_calls()) == 2, "the invalid lender is replaced before any output reaches the app"
    assert world.count("POST", "/jobs/stream") == 0, "the healthy second lender answers directly"
    for text in ("EVIL", "PRIVATE_LENDER", "hostname", "tool_calls", "reasoning", "fingerprint"):
        assert text not in resp.text


# ---- an unreachable coordinator gets its own error, and no contributor is blamed -------------------------------

import pytest  # noqa: E402

UNREACHABLE = {"error": {"category": "coordinator_unreachable", "message": "Could not reach the Potluck coordinator."}}
INTERRUPTED = {"error": {"category": "stream_interrupted", "message": "The connection ended before the reply finished."}}


def refuse(exc):
    def relay(body):
        raise exc

    return relay


def route_log(caplog):
    """The route's failure log lines. (Its other line, circle_chat_started, carries a count only.)"""
    return [r.getMessage() for r in caplog.records
            if r.name == "potluck_local.routers.chat_circle" and r.getMessage().startswith("circle_chat_failed")]


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("EVILHOST 10.9.8.7 refused"),
    httpx.ConnectTimeout("EVILHOST timed out"),
    httpx.ReadTimeout("EVILHOST"),
    httpx.RemoteProtocolError("EVILHOST"),
    ConnectionRefusedError("EVILHOST"),
    OSError("EVILHOST"),
    TimeoutError("EVILHOST"),
], ids=lambda e: type(e).__name__)
def test_a_coordinator_that_cannot_be_reached_before_the_first_event_is_its_own_error(world, monkeypatch, caplog, exc):
    from potluck_local.services import circle_link

    caplog.set_level("INFO")
    monkeypatch.setattr(circle_link, "direct_enabled", lambda: False)  # direct is not attempted
    world.relay = refuse(exc)
    resp = client().post("/chat/circle", json=APP_BODY)
    assert resp.status_code == 200
    assert frames(resp) == [UNREACHABLE, "[DONE]"], "exactly one error event, then the end"
    assert route_log(caplog) == [f"circle_chat_failed error={type(exc).__name__}"], "the class name and nothing else"
    assert "EVILHOST" not in caplog.text and "EVILHOST" not in resp.text


def test_the_same_after_a_direct_attempt_that_failed(world, caplog):
    caplog.set_level("INFO")
    world.lender_default = lambda request, link: (_ for _ in ()).throw(httpx.ConnectError("EVILHOST lender refused"))
    world.relay = refuse(httpx.ConnectError("EVILHOST coordinator refused"))
    resp = client().post("/chat/circle", json=APP_BODY)
    assert frames(resp) == [UNREACHABLE, "[DONE]"]
    assert len(world.lender_calls()) == 2 and world.count("POST", "/links") == 2, "both lenders were tried first"
    assert route_log(caplog) == ["circle_chat_failed error=ConnectError"] and "EVILHOST" not in caplog.text


def test_the_same_when_the_coordinator_is_down_for_the_link_call_too(world):
    world.create = refuse(httpx.ConnectError("refused"))
    world.relay = refuse(httpx.ConnectError("refused"))
    assert frames(client().post("/chat/circle", json=APP_BODY)) == [UNREACHABLE, "[DONE]"]
    assert world.lender_calls() == []


def test_a_relay_stream_that_breaks_after_events_began_is_stream_interrupted(world, monkeypatch, caplog):
    from potluck_local.services import circle_link

    caplog.set_level("INFO")
    monkeypatch.setattr(circle_link, "direct_enabled", lambda: False)
    world.relay = lambda body: stream_response(
        sse({"choices": [{"delta": {"content": "Rel"}}]}), sse({"choices": [{"delta": {"content": "ay"}}]}),
        httpx.ReadError("EVILHOST connection reset"))
    resp = client().post("/chat/circle", json=APP_BODY)
    assert frames(resp) == [{"choices": [{"delta": {"content": "Rel"}}]}, {"choices": [{"delta": {"content": "ay"}}]},
                            INTERRUPTED, "[DONE]"], "the tokens already sent, then one error"
    assert route_log(caplog) == ["circle_chat_failed error=ReadError"] and "EVILHOST" not in caplog.text


def test_a_relay_that_drops_after_the_first_event_and_not_before_is_interrupted_not_unreachable(world, monkeypatch):
    from potluck_local.services import circle_link

    monkeypatch.setattr(circle_link, "direct_enabled", lambda: False)
    world.relay = lambda body: stream_response(
        sse({"choices": [{"delta": {"content": "R"}}]}), OSError("reset"))
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[-2] == INTERRUPTED and UNREACHABLE not in events


def test_a_direct_links_connection_refused_on_both_attempts_gives_the_relays_reply_not_an_error(world):
    world.lender_default = lambda request, link: (_ for _ in ()).throw(httpx.ConnectError("refused"))
    events = frames(client().post("/chat/circle", json=APP_BODY))
    assert events[0] == {"choices": [{"delta": {"content": "Relay"}}]} and events[-2]["meta"]["path"] == "relay"
    assert not any("error" in e for e in events if isinstance(e, dict))


@pytest.mark.parametrize("failure,category", [
    (httpx.ReadError("EVILHOST"), "peer_unreachable"),
    (httpx.RemoteProtocolError("EVILHOST"), "peer_unreachable"),
    (OSError("EVILHOST"), "peer_unreachable"),
])
def test_a_direct_link_failing_after_the_first_token_is_never_coordinator_unreachable(world, failure, category):
    world.lender["100.64.1.2"] = lambda r, l: stream_response(content("Hel"), failure)
    resp = client().post("/chat/circle", json=APP_BODY)
    events = frames(resp)
    errors = [e["error"] for e in events if isinstance(e, dict) and "error" in e]
    assert [e["category"] for e in errors] == [category] and errors[0]["category"] != "coordinator_unreachable"
    assert "EVILHOST" not in resp.text and events[0] == {"choices": [{"delta": {"content": "Hel"}}]}
    assert world.count("POST", "/jobs/stream") == 0, "the relay was not called to cover for it"


def test_a_direct_link_that_goes_quiet_after_the_first_token_is_stream_timeout(world, monkeypatch):
    # The idle timer cancels the task that reads the stream. If the handler read
    # the first event and the response body read the rest, the timer would cancel
    # the handler and the error event and [DONE] would be lost (a short reply that
    # looks finished). One reader task reports the timeout in the stream.
    from potluck_local.services import circle_link

    monkeypatch.setattr(circle_link, "IDLE_TIMEOUT_S", 0.3)
    world.lender["100.64.1.2"] = lambda r, l: stream_response(content("Hel"), 5.0, content("lo"), meta(), DONE)
    events = frames(client().post("/chat/circle", json=APP_BODY))
    errors = [e["error"]["category"] for e in events if isinstance(e, dict) and "error" in e]
    assert errors == ["stream_timeout"]


def test_other_exceptions_keep_the_generic_error(world, monkeypatch, caplog):
    from potluck_local.services import pool_routing

    caplog.set_level("INFO")

    async def broken(*a, **kw):
        raise ValueError("EVILTEXT")
        yield  # pragma: no cover

    monkeypatch.setattr(pool_routing, "stream_pool_openai", broken)
    resp = client().post("/chat/circle", json=APP_BODY)
    assert frames(resp) == [{"error": {"category": "inference_error", "message": "The request failed."}}, "[DONE]"]
    assert "EVILTEXT" not in caplog.text and "EVILTEXT" not in resp.text


def test_a_chunk_the_route_cannot_read_ends_the_reply_with_an_error_event(world, monkeypatch, caplog):
    from potluck_local.services import pool_routing

    caplog.set_level("INFO")

    async def odd_stream(*a, **kw):
        yield {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]}
        yield {"EVILKEY": "EVILTEXT"}

    monkeypatch.setattr(pool_routing, "stream_pool_openai", odd_stream)
    resp = client().post("/chat/circle", json=APP_BODY)
    assert frames(resp) == [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"error": {"category": "inference_error", "message": "The request failed."}},
        "[DONE]",
    ]
    assert route_log(caplog) == ["circle_chat_failed error=KeyError"]
    assert "EVIL" not in resp.text and "EVIL" not in caplog.text


def test_an_http_error_from_the_coordinator_is_still_its_status_and_detail(world, monkeypatch):
    from potluck_local.services import circle_link

    monkeypatch.setattr(circle_link, "direct_enabled", lambda: False)
    world.relay = lambda body: httpx.Response(503, json={"detail": "No machine in this circle is online."})
    resp = client().post("/chat/circle", json=APP_BODY)
    assert resp.status_code == 503 and resp.json() == {"detail": "No machine in this circle is online."}


def test_a_remote_stream_error_keeps_its_category_and_text(world, monkeypatch):
    from potluck_local.services import circle_link

    monkeypatch.setattr(circle_link, "direct_enabled", lambda: False)
    world.relay = lambda body: stream_response(error_event("timeout", "Peer didn't complete within 120s"), DONE)
    assert frames(client().post("/chat/circle", json=APP_BODY)) == [
        {"error": {"category": "timeout", "message": "Peer didn't complete within 120s"}}, "[DONE]"]
