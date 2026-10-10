"""The circle listener's app and its gate.

The lender's side of a direct circle link. The gate has four rules (accepted on
this machine's own mesh address, source is a reachable /circle-peers entry whose
inbound is set, the bearer token's SHA-256 matches that entry, sharing is on); each
is tried alone and together. Starlette's test client sets the source address with
client=(ip, port) and the accepted address through base_url, as test_peer_trust.py
does; the `fake_mesh` fixture (conftest.py) stands in for the sidecar.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import itertools
import json
import time
from pathlib import Path

import pytest
from circle_support import FIXTURE, MODEL
from fastapi.testclient import TestClient

from potluck_local.routers import circle_link as router
from potluck_local.routers.circle_link import CircleDeps, create_circle_app
from potluck_local.services import circle_gate
from potluck_local.services.circle_gate import CircleRoster, DirectAdmission
from potluck_local.services.model_identity import ModelIdentityError

OWN = "100.64.0.4"
PEER = "100.64.1.2"
OTHER = "100.64.1.3"
TAILSCALE = "100.101.102.103"
LAN = "192.168.1.50"
TOKEN = FIXTURE["http"]["create_response"]["token"]
DIGEST = FIXTURE["signal"]["circle_peer_add_lender"]["token_sha256"]
LINK = "c0ffee00-1111-4222-8333-444455556666"
PORT = 40000
CHAT = {"messages": [{"role": "user", "content": "hi"}], "model": MODEL, "stream": True,
        "temperature": 0.7, "max_tokens": 64, "stop": None}


def inbound(token: str = TOKEN, link: str = LINK) -> dict:
    return {"link_id": link, "port": 8340, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_in_s": 400}


class Runtime:
    """A stand-in for the engine's model runtime."""

    def __init__(self, model: str | None = MODEL, words=("Hel", "lo")) -> None:
        self.model = model
        self.words = list(words)
        self.calls: list[dict] = []
        self.closed = 0
        self.gate = None  # an asyncio.Event the stream waits on after its first word
        self.forever = False
        self.verified = True  # the answer check (services/acceptance.py)

    def answers_verified(self):
        return self.verified if self.model is not None else None

    def is_loaded(self) -> bool:
        return self.model is not None

    def current_model_id(self):
        return self.model

    async def stream_chat_raw(self, messages, *, temperature, max_tokens, stop, expected_model=None,
                              expected_sha256=None):
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                           "stop": stop, "expected_model": expected_model})
        if expected_model is not None and expected_model != self.model:
            raise ModelIdentityError("changed")
        try:
            yield {"choices": [{"delta": {"role": "assistant"}}]}
            for i, word in enumerate(self.words):
                if i == 1 and self.gate is not None:
                    await self.gate.wait()
                await asyncio.sleep(0)
                yield {"choices": [{"delta": {"content": word}}]}
            n = 0
            while self.forever:
                n += 1
                await asyncio.sleep(0.03)
                yield {"choices": [{"delta": {"content": f"w{n}"}}]}
        finally:
            self.closed += 1


class Lender:
    """Everything the listener needs from the engine, adjustable by a test."""

    def __init__(self, fake_mesh, clock=None) -> None:
        self.mesh = fake_mesh
        fake_mesh.own_ip = OWN
        fake_mesh.enable_links()
        fake_mesh.add_circle_peer(PEER, inbound=inbound())
        self.runtime = Runtime()
        self.cap: int | None = 2
        self.relay = 0
        self.refusal: str | None = None
        self.limit: int | None = None
        self.roster = CircleRoster(clock=clock) if clock else CircleRoster()
        self.admission = DirectAdmission(cap=lambda: self.cap, other_in_flight=lambda: self.relay)
        self.deps = CircleDeps(
            roster=self.roster, admission=self.admission, sharing_refusal=lambda: self.refusal,
            max_tokens_limit=lambda: self.limit, runtime=lambda: self.runtime)
        self.app = create_circle_app(self.deps)

    def client(self, source: str = PEER, accepted: str = OWN) -> TestClient:
        return TestClient(self.app, client=(source, PORT), base_url=f"http://{accepted}:8340",
                          raise_server_exceptions=False)


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


ROWS: list[dict] = []


@pytest.fixture(autouse=True)
def usage_rows(monkeypatch):
    """The metadata-only usage rows the listener leaves, kept in memory."""
    from potluck_local.services import usage_store

    ROWS.clear()

    async def record(**kwargs):
        ROWS.append(kwargs)

    monkeypatch.setattr(usage_store, "record_usage_event", record)
    return ROWS


@pytest.fixture()
def lender(fake_mesh):
    return Lender(fake_mesh)


def events(response) -> list:
    out = []
    for line in response.text.split("\n\n"):
        if line.startswith("data: "):
            body = line[6:]
            out.append(body if body == "[DONE]" else json.loads(body))
    return out


# ---- the gate: each rule alone and together ----------------------------------


def test_a_circle_peer_with_the_right_token_is_served(lender):
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert events(resp)[-1] == "[DONE]"


def refusal_for(lender, *, accepted_ok, listed, token_ok, sharing_ok):
    if not listed:
        lender.mesh.remove_circle_peer(PEER)
    lender.refusal = None if sharing_ok else "not_sharing_circles"
    client = lender.client(accepted=OWN if accepted_ok else TAILSCALE)
    resp = client.post("/circle/chat/completions", json=CHAT, headers=auth(TOKEN if token_ok else "wrong-token"))
    return resp


@pytest.mark.parametrize("accepted_ok,listed,token_ok,sharing_ok",
                         list(itertools.product([True, False], repeat=4)))
def test_only_all_four_rules_together_let_a_caller_in(fake_mesh, accepted_ok, listed, token_ok, sharing_ok):
    lender = Lender(fake_mesh)
    resp = refusal_for(lender, accepted_ok=accepted_ok, listed=listed, token_ok=token_ok, sharing_ok=sharing_ok)
    if not accepted_ok or not listed:
        assert resp.status_code == 403 and resp.json()["detail"]["code"] == "not_a_circle_peer"
    elif not token_ok:
        assert resp.status_code == 401 and resp.json()["detail"]["code"] == "bad_token"
    elif not sharing_ok:
        assert resp.status_code == 503 and resp.json()["detail"]["code"] == "not_sharing"
    else:
        assert resp.status_code == 200
    assert (lender.runtime.calls != []) == (resp.status_code == 200)


def test_a_stranger_learns_nothing_about_whether_the_machine_is_sharing(lender):
    lender.refusal = "not_sharing_circles"
    stranger = lender.client(TAILSCALE).post("/circle/chat/completions", json=CHAT, headers=auth())
    wrong_address = lender.client(PEER, accepted=LAN).post("/circle/chat/completions", json=CHAT, headers=auth())
    assert stranger.status_code == wrong_address.status_code == 403
    assert stranger.json() == wrong_address.json()


@pytest.mark.parametrize("accepted", [TAILSCALE, "127.0.0.1", LAN, "0.0.0.0", "testserver"])
def test_the_right_source_on_a_different_local_address_is_refused(lender, accepted):
    resp = lender.client(PEER, accepted=accepted).post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == 403


def test_a_forged_host_header_does_not_change_the_accepted_address(lender):
    resp = lender.client(PEER, accepted=TAILSCALE).post(
        "/circle/chat/completions", json=CHAT, headers={**auth(), "Host": f"{OWN}:8340"})
    assert resp.status_code == 403


def test_forwarding_headers_do_not_change_the_source(lender):
    resp = lender.client(TAILSCALE).post(
        "/circle/chat/completions", json=CHAT,
        headers={**auth(), "X-Forwarded-For": PEER, "X-Real-IP": PEER, "Forwarded": f"for={PEER}"})
    assert resp.status_code == 403


def test_missing_server_or_client_address_is_refused(lender):
    async def call(client, server):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/circle/capabilities", "raw_path": b"/circle/capabilities",
                 "query_string": b"", "headers": [(b"host", b"x"), (b"authorization", f"Bearer {TOKEN}".encode())],
                 "client": client, "server": server}
        await lender.app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    for server in (None, ("/run/engine.sock", None), (), ("", 8340), (OWN,), (OWN, 8340, 0), "100.64.0.4", 7):
        assert asyncio.run(call((PEER, PORT), server)) == 403, server
    for client in (None, (), ("", PORT), (PEER,), "100.64.1.2"):
        assert asyncio.run(call(client, (OWN, 8340))) == 403, client
    assert asyncio.run(call((PEER, PORT), (OWN, 8340))) == 200
    assert asyncio.run(call((PEER, PORT), [OWN, 8340])) == 200


def test_this_machines_own_mesh_address_as_source_is_refused(fake_mesh):
    lender = Lender(fake_mesh)
    fake_mesh.add_circle_peer(OWN, inbound=inbound())
    assert lender.client(OWN, accepted=OWN).get("/circle/capabilities", headers=auth()).status_code == 403


def test_a_peer_we_only_borrow_from_may_not_call_us(lender):
    # An outbound-only entry is a machine WE may call. It gives it no right to call us.
    lender.mesh.remove_circle_peer(PEER)
    lender.mesh.add_circle_peer(PEER, outbound={"link_id": LINK, "port": 8340, "expires_in_s": 100})
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


@pytest.mark.parametrize("reachable", [False, "true", 1, None, "yes", [True]])
def test_an_inbound_peer_that_is_not_reachable_is_refused(fake_mesh, reachable):
    lender = Lender(fake_mesh)
    fake_mesh.remove_circle_peer(PEER)
    fake_mesh.add_circle_peer(PEER, reachable=reachable, inbound=inbound())
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


def test_a_household_peer_is_not_a_circle_peer(lender):
    lender.mesh.remove_circle_peer(PEER)
    lender.mesh.add_peer(PEER)  # in /peers (the household), not in /circle-peers
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


@pytest.mark.parametrize("scope", [None, "household", "pool", "", 1])
def test_only_scope_circle_counts(fake_mesh, scope):
    lender = Lender(fake_mesh)
    fake_mesh.circle_peers[0]["scope"] = scope
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
    {"Authorization": f"Basic {TOKEN}"},
    {"Authorization": TOKEN},
    {"Authorization": f"Bearer {TOKEN}x"},
    {"Authorization": f"Bearer x{TOKEN}"},
    {"Authorization": f"Bearer {TOKEN[:-1]}"},
    {"Authorization": f"Bearer {DIGEST}"},              # the hash is not the token
    {"Authorization": f"Bearer {'a' * 5000}"},          # not even hashed
    {"Authorization": "Bearer café".encode("latin-1")},  # not ASCII
    {"Authorization": f"Bearer {TOKEN.upper()}"},
    {"X-Authorization": f"Bearer {TOKEN}"},
    {"Cookie": f"token={TOKEN}"},
])
def test_a_missing_or_wrong_token_is_refused(lender, headers):
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=headers)
    assert resp.status_code == 401 and resp.json() == FIXTURE["engine"]["errors"]["bad_token"]["body"]
    assert lender.runtime.calls == []


def test_the_token_is_compared_by_its_hash_in_constant_time(monkeypatch, lender):
    compared = []
    real = circle_gate.hmac.compare_digest
    monkeypatch.setattr(circle_gate.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real(a, b))
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 200
    expected = hashlib.sha256(TOKEN.encode()).hexdigest()
    assert compared == [(expected, expected)]


def test_the_scheme_may_be_any_case(lender):
    resp = lender.client().get("/circle/capabilities", headers={"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200


def test_the_entrys_own_hash_is_what_counts_whatever_its_case(fake_mesh):
    lender = Lender(fake_mesh)
    fake_mesh.circle_peers[0]["inbound"]["token_sha256"] = hashlib.sha256(TOKEN.encode()).hexdigest().upper()
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 200


@pytest.mark.parametrize("bad", [None, "", "abc", "g" * 64, "a" * 63, "a" * 65, 12345, [], {"x": 1}])
def test_an_inbound_entry_without_a_proper_hash_lets_nobody_in(fake_mesh, bad):
    lender = Lender(fake_mesh)
    fake_mesh.circle_peers[0]["inbound"]["token_sha256"] = bad
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


def test_two_entries_for_one_address_let_nobody_in(fake_mesh):
    lender = Lender(fake_mesh)
    fake_mesh.add_circle_peer(PEER, inbound=inbound("another-token-another-token-12345"))
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403


# ---- the sidecar cannot be trusted ----------------------------------------------


@pytest.mark.parametrize("break_it", [
    lambda m: setattr(m, "down", True),
    lambda m: setattr(m, "status_body", ["junk"]),
    lambda m: setattr(m, "status_body", {"mesh_ip": "not-an-ip"}),
    lambda m: setattr(m, "status_body", {"mesh_ip": 1234}),
    lambda m: setattr(m, "circle_peers_body", "junk"),
    lambda m: setattr(m, "circle_peers_body", {"peers": "junk"}),
    lambda m: setattr(m, "circle_peers_body", {"nope": []}),
    lambda m: setattr(m, "circle_peers_enabled", False),       # a sidecar without /circle-peers
], ids=["down", "junk_status", "bad_ip", "int_ip", "junk_peers", "peers_not_list", "no_peers_key", "no_endpoint"])
def test_a_sidecar_that_is_down_or_answers_junk_means_everyone_is_refused(fake_mesh, break_it):
    lender = Lender(fake_mesh)
    break_it(fake_mesh)
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 403
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 403
    assert lender.runtime.calls == []


def test_junk_entries_are_dropped_and_the_good_one_still_works(fake_mesh):
    lender = Lender(fake_mesh)
    fake_mesh.circle_peers[:0] = ["junk", 7, None, {"scope": "circle"}, {"scope": "circle", "mesh_ip": 5}]
    assert lender.client().get("/circle/capabilities", headers=auth()).status_code == 200


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_the_sidecar_is_read_once_a_second_not_once_a_request(fake_mesh):
    clock = Clock()
    lender = Lender(fake_mesh, clock)
    client = lender.client()
    for _ in range(5):
        assert client.get("/circle/capabilities", headers=auth()).status_code == 200
    assert fake_mesh.calls.count("/circle-peers") == 1
    clock.advance(1.5)
    assert client.get("/circle/capabilities", headers=auth()).status_code == 200
    assert fake_mesh.calls.count("/circle-peers") == 2


def test_an_ended_link_stops_working_within_the_cache_time(fake_mesh):
    clock = Clock()
    lender = Lender(fake_mesh, clock)
    client = lender.client()
    assert client.get("/circle/capabilities", headers=auth()).status_code == 200
    fake_mesh.remove_circle_peer(PEER)
    clock.advance(1.1)
    assert client.get("/circle/capabilities", headers=auth()).status_code == 403


def test_a_link_made_a_moment_ago_does_not_wait_out_the_cache(fake_mesh):
    clock = Clock()
    lender = Lender(fake_mesh, clock)
    fake_mesh.remove_circle_peer(PEER)
    client = lender.client()
    assert client.get("/circle/capabilities", headers=auth()).status_code == 403
    fake_mesh.add_circle_peer(PEER, inbound=inbound())
    clock.advance(0.3)  # past the read limit, inside the cache time
    assert client.get("/circle/capabilities", headers=auth()).status_code == 200


def test_the_first_request_on_a_link_made_a_moment_after_a_read_is_not_turned_away(fake_mesh):
    # A read just before the link was installed is in the cache; the request that
    # follows 30 ms later (the shortest a coordinator round trip leaves) gets a fresh look.
    clock = Clock()
    lender = Lender(fake_mesh, clock)
    fake_mesh.remove_circle_peer(PEER)
    client = lender.client()
    assert client.get("/circle/capabilities", headers=auth()).status_code == 403
    fake_mesh.add_circle_peer(PEER, inbound=inbound())
    clock.advance(0.03)
    assert client.get("/circle/capabilities", headers=auth()).status_code == 200


def test_a_replaced_link_does_not_turn_its_first_request_away_either(fake_mesh):
    clock = Clock()
    lender = Lender(fake_mesh, clock)
    client = lender.client()
    assert client.get("/circle/capabilities", headers=auth()).status_code == 200  # the old link, cached
    new_token = "a-brand-new-token-a-brand-new-token"
    fake_mesh.circle_peers[0]["inbound"] = inbound(new_token, "d00dfeed-1111-4222-8333-444455556666")
    clock.advance(0.03)
    assert client.get("/circle/capabilities", headers=auth(new_token)).status_code == 200


def test_callers_refused_for_where_they_came_from_cause_no_extra_read(fake_mesh):
    lender = Lender(fake_mesh)
    reads = lambda: fake_mesh.calls.count("/circle-peers")  # noqa: E731
    lender.client(PEER, accepted=LAN).get("/circle/capabilities", headers=auth())  # the cache is filled
    before = reads()
    for _ in range(20):
        lender.client(PEER, accepted=LAN).get("/circle/capabilities", headers=auth())
        lender.client(OWN, accepted=OWN).get("/circle/capabilities", headers=auth())
    assert reads() == before


def test_many_strangers_share_one_read(fake_mesh):
    lender = Lender(fake_mesh)
    fake_mesh.delay = 0.05

    async def go():
        async def call():
            return await lender.roster.refusal_reason(TAILSCALE, OWN, TOKEN)

        return await asyncio.gather(*(call() for _ in range(20)))

    assert set(asyncio.run(go())) == {"not_inbound"}
    assert fake_mesh.calls.count("/circle-peers") <= 2


# ---- no other path exists -------------------------------------------------------


PATHS = ["/peer/capabilities", "/peer/chat/completions", "/peer/memory/search", "/health", "/v1/models",
         "/v1/chat/completions", "/memory/search", "/docs", "/openapi.json", "/redoc", "/", "/circle", "/circle/",
         "/circle/capabilities/", "/circle/chat", "/circle//capabilities", "/circle/../peer/capabilities",
         "/CIRCLE/capabilities", "/circle/capabilities%2f", "/chat/circle", "/contributor/status", "/settings"]


@pytest.mark.parametrize("creds", ["right", "wrong", "none"])
@pytest.mark.parametrize("path", PATHS)
def test_every_other_path_is_404_whoever_asks(lender, path, creds):
    headers = {"right": auth(), "wrong": auth("nope"), "none": {}}[creds]
    client = lender.client(PEER if creds != "none" else TAILSCALE)
    for method in ("GET", "POST", "PUT", "DELETE"):
        assert client.request(method, path, headers=headers, json={}).status_code == 404, (method, path)
    assert lender.runtime.calls == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/circle/capabilities"), ("GET", "/circle/chat/completions"),
    ("PUT", "/circle/chat/completions"), ("DELETE", "/circle/chat/completions"),
    ("PATCH", "/circle/capabilities"), ("HEAD", "/circle/capabilities"), ("OPTIONS", "/circle/capabilities"),
    ("OPTIONS", "/circle/chat/completions"),
])
def test_a_wrong_method_on_a_known_path_is_404_too(lender, method, path):
    assert lender.client().request(method, path, headers=auth()).status_code == 404


def test_the_listener_app_has_only_two_routes(lender):
    from starlette.applications import Starlette
    from starlette.routing import Route

    inner = lender.app.app
    assert isinstance(inner, Starlette)
    assert sorted((r.path, sorted(r.methods - {"HEAD"})) for r in inner.routes if isinstance(r, Route)) == [
        ("/circle/capabilities", ["GET"]), ("/circle/chat/completions", ["POST"])]
    assert len(inner.routes) == 2


def test_websocket_upgrades_are_refused(lender):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with lender.client().websocket_connect("/circle/chat/completions"):
            pass


# ---- the main port keeps its door shut ---------------------------------------------


@pytest.fixture()
def engine(fake_mesh):
    from potluck_local.app import create_app

    fake_mesh.own_ip = OWN
    fake_mesh.enable_links()
    fake_mesh.add_circle_peer(PEER, inbound=inbound())
    fake_mesh.add_peer(PEER)
    return create_app()


@pytest.mark.parametrize("source", [PEER, TAILSCALE, LAN, OTHER])
@pytest.mark.parametrize("path", ["/circle/capabilities", "/circle/chat/completions", "/chat/circle"])
def test_circle_routes_on_the_main_port_are_unavailable_from_another_machine(engine, source, path):
    client = TestClient(engine, client=(source, PORT), base_url=f"http://{OWN}:8321", raise_server_exceptions=False)
    resp = client.request("POST" if "chat" in path else "GET", path, headers=auth(), json=CHAT)
    assert resp.status_code == 403
    assert "localhost" in resp.json()["detail"]


def test_circle_routes_do_not_exist_on_the_main_app_even_for_this_machine(engine):
    client = TestClient(engine, client=("127.0.0.1", PORT), raise_server_exceptions=False)
    assert client.get("/circle/capabilities", headers=auth()).status_code == 404
    assert client.post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 404


def test_the_household_gate_is_untouched_by_circle_peers(engine):
    # A circle peer that is also listed as a household peer address is judged by
    # the household's rules on /peer/*, which still work.
    client = TestClient(engine, client=(PEER, PORT), base_url=f"http://{OWN}:8321", raise_server_exceptions=False)
    assert client.get("/peer/capabilities").status_code == 200
    assert client.get("/peer/capabilities").json().get("hostname")


def test_a_circle_only_peer_cannot_reach_peer_routes_on_the_main_port(fake_mesh):
    from potluck_local.app import create_app

    fake_mesh.own_ip = OWN
    fake_mesh.enable_links()
    fake_mesh.add_circle_peer(PEER, inbound=inbound())  # a circle peer only; /peers is empty
    client = TestClient(create_app(), client=(PEER, PORT), base_url=f"http://{OWN}:8321", raise_server_exceptions=False)
    assert client.get("/peer/capabilities").status_code == 403
    assert client.post("/peer/chat/completions", json=CHAT).status_code == 403


# ---- what a request may be -----------------------------------------------------------


def post_raw(lender, body: bytes, headers=None):
    return lender.client().post("/circle/chat/completions", content=body,
                                headers={**auth(), "Content-Type": "application/json", **(headers or {})})


@pytest.mark.parametrize("change", [
    {"stream": False},
    {"stream": "yes"},
    {"potluck_batch": True},
    {"potluck_model_sha256": "a" * 64},
    {"conversation_id": "x"},
    {"extra": 1},
    {"model": None},
    {"model": ""},
    {"model": 5},
    {"max_tokens": 4097},
    {"max_tokens": 0},
    {"max_tokens": "512"},
    {"temperature": 2.5},
    {"temperature": -1},
    {"temperature": "0.7"},
    {"messages": []},
    {"messages": "hi"},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user"}]},
    {"messages": [{"role": "user", "content": "x", "name": "n"}]},
    {"messages": [{"role": "user", "content": 5}]},
    {"stop": "x"},
    {"stop": ["a"] * 9},
])
def test_a_request_outside_the_contract_is_422_and_never_reaches_the_model(lender, change):
    body = {**CHAT, **change}
    for key in [k for k, v in change.items() if v is None and k == "model"]:
        del body[key]
    resp = lender.client().post("/circle/chat/completions", json=body, headers=auth())
    assert resp.status_code == 422, change
    assert resp.json()["detail"]["code"] == "invalid_request"
    assert lender.runtime.calls == []


def test_a_missing_model_is_422(lender):
    body = {k: v for k, v in CHAT.items() if k != "model"}
    assert lender.client().post("/circle/chat/completions", json=body, headers=auth()).status_code == 422


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b'{"messages": ', b"\xff\xfe", b"{" * 5000])
def test_garbage_is_422(lender, raw):
    assert post_raw(lender, raw).status_code == 422


def test_a_refusal_does_not_echo_the_request_back(lender):
    resp = lender.client().post(
        "/circle/chat/completions", headers=auth(),
        json={**CHAT, "messages": [{"role": "user", "content": "my-very-private-question-123"}], "stream": False})
    assert resp.status_code == 422 and "my-very-private-question-123" not in resp.text


def test_omitting_stop_and_stream_is_fine(lender):
    body = {"messages": CHAT["messages"], "model": MODEL, "max_tokens": 8}
    assert lender.client().post("/circle/chat/completions", json=body, headers=auth()).status_code == 200


def test_a_body_that_is_too_large_is_413_before_it_is_parsed(lender):
    big = json.dumps({**CHAT, "messages": [{"role": "user", "content": "x" * (router.MAX_BODY_BYTES + 10)}]}).encode()
    resp = post_raw(lender, big)
    assert resp.status_code == 413 and resp.json()["detail"]["code"] == "request_too_large"
    assert lender.runtime.calls == []


def test_a_body_that_is_too_large_is_413_even_when_it_does_not_say_how_long_it_is(lender):
    def chunks():
        yield b'{"messages": [{"role": "user", "content": "'
        for _ in range(router.MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536
        yield b'"}]}'

    resp = lender.client().post("/circle/chat/completions", content=chunks(), headers=auth())
    assert resp.status_code == 413


def test_no_model_loaded_is_503(lender):
    lender.runtime.model = None
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert (resp.status_code, resp.json()) == (503, FIXTURE["engine"]["errors"]["no_model"]["body"])


def test_another_model_is_409(lender):
    resp = lender.client().post("/circle/chat/completions", json={**CHAT, "model": "other"}, headers=auth())
    assert (resp.status_code, resp.json()) == (409, FIXTURE["engine"]["errors"]["model_mismatch"]["body"])


def test_the_model_is_checked_again_when_the_runtime_takes_the_request(lender):
    # The model is swapped between the check and the stream: the runtime refuses.
    class Swapped(Runtime):
        def current_model_id(self):
            return self.model if not self.calls else "swapped"

        async def stream_chat_raw(self, messages, **kw):
            self.model = "swapped"
            async for chunk in super().stream_chat_raw(messages, **kw):
                yield chunk

    lender.runtime = Swapped()
    lender.deps.runtime = lambda: lender.runtime
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    frames = events(resp)
    assert frames[0]["error"]["category"] == "model_mismatch" and frames[-1] == "[DONE]"


def test_sharing_off_is_503(lender):
    lender.refusal = "not_sharing_circles"
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert (resp.status_code, resp.json()) == (503, FIXTURE["engine"]["errors"]["not_sharing"]["body"])


def test_contributor_max_tokens_applies(lender):
    lender.limit = 100
    assert lender.client().post("/circle/chat/completions", json={**CHAT, "max_tokens": 100}, headers=auth()).status_code == 200
    resp = lender.client().post("/circle/chat/completions", json={**CHAT, "max_tokens": 101}, headers=auth())
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "max_tokens_over_limit"
    assert len(lender.runtime.calls) == 1


# ---- capacity: direct and relay jobs share one cap ---------------------------------------


def test_direct_and_relay_jobs_share_one_cap(lender):
    lender.cap = 2
    lender.relay = 1
    slot = lender.admission.try_acquire()  # one direct job is running
    assert slot is not None and lender.admission.snapshot() == (2, 2)
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert (resp.status_code, resp.json(), resp.headers["retry-after"]) == (
        429, FIXTURE["engine"]["errors"]["busy"]["body"], "2")
    slot.release()
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 200


def test_relay_jobs_fill_the_cap_before_a_direct_request_arrives(lender):
    lender.cap, lender.relay = 1, 1
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 429
    assert lender.runtime.calls == []


def test_a_cap_of_zero_serves_nothing(lender):
    lender.cap = 0
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 429


def test_the_cap_follows_the_setting_live(lender):
    lender.cap = 1
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 200
    lender.cap = None  # uncapped
    slots = [lender.admission.try_acquire() for _ in range(10)]
    assert all(slots)


def test_a_finished_request_gives_its_place_back(lender):
    lender.cap = 1
    for _ in range(3):
        assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 200
        assert lender.admission.in_flight == 0


def test_a_failed_stream_gives_its_place_back(lender):
    class Broken(Runtime):
        async def stream_chat_raw(self, messages, **kw):
            raise RuntimeError("model exploded: prompt text must not escape")
            yield  # pragma: no cover

    lender.runtime = Broken()
    lender.deps.runtime = lambda: lender.runtime
    lender.cap = 1
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    frames = events(resp)
    assert frames[0] == {"error": {"category": "inference_error", "message": "Inference failed"}}
    assert "exploded" not in resp.text and frames[-1] == "[DONE]"
    assert lender.admission.in_flight == 0


def test_a_runtime_error_chunk_is_reported_without_its_text(lender):
    class Errors(Runtime):
        async def stream_chat_raw(self, messages, **kw):
            yield {"choices": [{"delta": {"content": "Hel"}}]}
            yield {"__error__": "secret internals"}

    lender.runtime = Errors()
    lender.deps.runtime = lambda: lender.runtime
    frames = events(lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()))
    assert frames[0]["choices"][0]["delta"]["content"] == "Hel"
    assert frames[1] == {"error": {"category": "inference_error", "message": "Inference failed"}}
    assert "secret" not in json.dumps(frames) and lender.admission.in_flight == 0


# ---- the answer ----------------------------------------------------------------------------


def test_the_stream_is_the_contracts(lender):
    lender.runtime = Runtime(words=("Hello",))
    lender.deps.runtime = lambda: lender.runtime
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    lines = [line for line in resp.text.split("\n\n") if line]
    shape = FIXTURE["engine"]["sse"]
    assert lines[0] == shape[0], "the chunk is byte for byte the fixture's"
    meta = json.loads(lines[1][6:])
    expected = json.loads(shape[1][6:])
    assert set(meta["meta"]) == set(expected["meta"])
    assert {k: v for k, v in meta["meta"].items() if k != "duration_ms"} == \
        {k: v for k, v in expected["meta"].items() if k not in ("duration_ms",)}
    assert type(meta["meta"]["duration_ms"]) is int
    assert lines[2] == shape[2]
    assert len(lines) == 3, "the role-only opening chunk is not forwarded"


def test_the_model_is_called_with_the_request_and_nothing_else(lender):
    body = {**CHAT, "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
            "temperature": 0.2, "max_tokens": 33, "stop": ["END"]}
    lender.client().post("/circle/chat/completions", json=body, headers=auth())
    (call,) = lender.runtime.calls
    assert call == {"messages": body["messages"], "temperature": 0.2, "max_tokens": 33, "stop": ["END"],
                    "expected_model": MODEL}


def test_finish_reason_length_when_the_limit_was_reached(lender):
    lender.runtime = Runtime(words=("a", "b", "c"))
    lender.deps.runtime = lambda: lender.runtime
    frames = events(lender.client().post("/circle/chat/completions", json={**CHAT, "max_tokens": 3}, headers=auth()))
    assert frames[-2]["meta"]["finish_reason"] == "length" and frames[-2]["meta"]["output_tokens"] == 3


def test_capabilities_are_the_contracts_and_leak_nothing(lender):
    resp = lender.client().get("/circle/capabilities", headers=auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body == FIXTURE["engine"]["capabilities"] | {"max_concurrent_jobs": 2}
    assert set(body) == set(FIXTURE["engine"]["capabilities"])
    text = resp.text.lower()
    for forbidden in ("hostname", "gpu", "hardware", "rpc", "tokens_per", "ram", "platform", "cpu"):
        assert forbidden not in text


def test_capabilities_say_busy_and_how_many_are_running(lender):
    lender.cap, lender.relay = 2, 1
    slot = lender.admission.try_acquire()
    body = lender.client().get("/circle/capabilities", headers=auth()).json()
    assert body["busy"] is True and body["in_flight"] == 2
    slot.release()
    lender.runtime.model = None
    body = lender.client().get("/circle/capabilities", headers=auth()).json()
    assert body["available_for_inference"] is False and body["loaded_model"] is None


# ---- privacy ------------------------------------------------------------------------------------


def test_nothing_is_persisted_injected_extracted_or_observed(lender, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a circle request must not touch the owner's memory, history or sessions")

    from potluck_local.routers import chat
    from potluck_local.services import agent_observability, memory_extraction, memory_retrieval, task_state

    monkeypatch.setattr(chat, "_build_enhanced_messages", boom)
    monkeypatch.setattr(chat, "_get_chat_session_id", boom)
    monkeypatch.setattr(memory_retrieval, "get_memory_retriever", boom)
    monkeypatch.setattr(memory_extraction, "get_memory_extractor", boom)
    monkeypatch.setattr(task_state, "get_task_manager", boom)
    monkeypatch.setattr(agent_observability, "get_observer", boom)
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 200


def test_the_router_imports_nothing_that_touches_memory_or_history():
    tree = ast.parse(Path(router.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("memory", "chat", "agent_observability", "task_state", "context_manager", "vault",
                 "conversation", "settings_manager", "peer_trust")
    leaks = sorted(i for i in imported if any(part in i.split(".")[-1] or part in i for part in forbidden)
                   if i.startswith("potluck_local") and "usage_store" not in i)
    assert leaks == [], leaks


def test_one_metadata_only_usage_row_is_left_per_served_request(lender, usage_rows):
    body = {**CHAT, "messages": [{"role": "user", "content": "my-very-private-question-123"}]}
    assert lender.client().post("/circle/chat/completions", json=body, headers=auth()).status_code == 200
    (row,) = usage_rows
    assert set(row) == {"direction", "scope", "model_slug", "output_tokens", "duration_ms", "ok"}
    assert row["direction"] == "served" and row["scope"] == "circle" and row["model_slug"] == MODEL
    assert row["output_tokens"] == 2 and row["ok"] is True
    assert "my-very-private-question-123" not in json.dumps(row)


def test_no_usage_row_for_a_request_that_failed(lender, usage_rows):
    lender.runtime.model = None
    lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert usage_rows == []


def test_logs_carry_counts_only(lender, caplog):
    caplog.set_level("DEBUG")
    body = {**CHAT, "messages": [{"role": "user", "content": "my-very-private-question-123"}]}
    lender.runtime = Runtime(words=("secret-answer-456",))
    lender.deps.runtime = lambda: lender.runtime
    lender.client().post("/circle/chat/completions", json=body, headers=auth())
    lender.client(TAILSCALE).post("/circle/chat/completions", json=body, headers=auth())
    lender.client().post("/circle/chat/completions", json=body, headers=auth("wrong-token-wrong-token"))
    assert "my-very-private-question-123" not in caplog.text
    assert "secret-answer-456" not in caplog.text
    assert TOKEN not in caplog.text and "wrong-token-wrong-token" not in caplog.text
    assert "message_count=1" in caplog.text and "output_tokens=1" in caplog.text


def test_refusals_are_logged_at_debug_only(lender, caplog):
    caplog.set_level("INFO")
    lender.client(TAILSCALE).post("/circle/chat/completions", json=CHAT, headers=auth())
    assert TAILSCALE not in caplog.text


# ---- a request that is running is watched -----------------------------------------------------------


async def call_asgi(app, *, client=(PEER, PORT), server=(OWN, 8340), body=None, on_send=None, disconnect=None,
                    path="/circle/chat/completions", method="POST"):
    raw = json.dumps(body or CHAT).encode()
    sent: list[dict] = []
    served = False

    async def receive():
        nonlocal served
        if not served:
            served = True
            return {"type": "http.request", "body": raw, "more_body": False}
        if disconnect is not None:
            await disconnect.wait()
        else:
            await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if on_send is not None:
            await on_send(message)
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"host", b"x"), (b"content-type", b"application/json"),
                         (b"authorization", f"Bearer {TOKEN}".encode())],
             "client": client, "server": server}
    await app(scope, receive, send)
    return sent


def body_text(sent) -> str:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()


@pytest.fixture()
def quick(monkeypatch):
    monkeypatch.setattr(router, "RECHECK_INTERVAL_S", 0.1)


def test_a_link_that_ends_mid_stream_cuts_the_stream_and_hands_the_model_back(lender, quick):
    lender.runtime.forever = True

    async def go():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.4)
        assert not task.done() and lender.admission.in_flight == 1
        lender.mesh.remove_circle_peer(PEER)
        removed = time.monotonic()
        sent = await asyncio.wait_for(task, 3)
        return sent, time.monotonic() - removed

    sent, took = asyncio.run(go())
    assert took < 0.8, "cut within a few checks of the link going"
    text = body_text(sent)
    assert "w1" in text and '"category":"link_ended"' in text and text.rstrip().endswith("[DONE]")
    assert lender.admission.in_flight == 0
    assert lender.runtime.closed == 1, "the model was released"


def test_the_real_cadence_cuts_within_about_two_seconds(lender):
    lender.runtime.forever = True

    async def go():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.3)
        lender.mesh.remove_circle_peer(PEER)
        removed = time.monotonic()
        await asyncio.wait_for(task, 4)
        return time.monotonic() - removed

    assert asyncio.run(go()) < 2.6


def test_replacing_a_link_does_not_cut_a_stream_on_the_old_one(lender, quick):
    lender.runtime.forever = True

    async def go():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.3)
        # The requester made a new link: a new token and link id for the same peer.
        lender.mesh.circle_peers[0]["inbound"] = inbound("a-brand-new-token-a-brand-new-token", "d00dfeed-1111-4222-8333-444455556666")
        await asyncio.sleep(0.6)
        assert not task.done(), "the old stream goes on"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert lender.admission.in_flight == 0


def test_sharing_turned_off_mid_stream_cuts_it(lender, quick):
    lender.runtime.forever = True

    async def go():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.3)
        lender.refusal = "disabled"
        return await asyncio.wait_for(task, 3)

    assert '"category":"link_ended"' in body_text(asyncio.run(go()))
    assert lender.admission.in_flight == 0


def test_a_sidecar_that_stays_unreadable_cuts_the_stream_but_one_bad_read_does_not(lender, monkeypatch):
    monkeypatch.setattr(router, "RECHECK_INTERVAL_S", 0.3)
    lender.runtime.forever = True

    async def blip():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.4)
        lender.mesh.down = True
        await asyncio.sleep(0.4)  # one check sees it down
        lender.mesh.down = False
        await asyncio.sleep(1.5)  # many good checks follow
        alive = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return alive

    assert asyncio.run(blip()), "a blip of the sidecar does not cut a good stream"

    async def outage():
        task = asyncio.ensure_future(call_asgi(lender.app))
        await asyncio.sleep(0.4)
        lender.mesh.down = True
        return await asyncio.wait_for(task, 4)

    assert '"category":"link_ended"' in body_text(asyncio.run(outage()))


def test_a_caller_that_stops_reading_cannot_hold_a_place(lender, quick):
    lender.runtime.forever = True
    blocked = asyncio.Event
    state = {}

    async def go():
        stuck = blocked()
        state["stuck"] = stuck

        async def on_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await stuck.wait()  # the peer's receive window is full: send never returns

        task = asyncio.ensure_future(call_asgi(lender.app, on_send=on_send))
        await asyncio.sleep(0.4)
        assert not task.done() and lender.admission.in_flight == 1
        lender.mesh.remove_circle_peer(PEER)
        started = time.monotonic()
        await asyncio.wait_for(task, 4)
        return time.monotonic() - started

    assert asyncio.run(go()) < 2.5
    assert lender.admission.in_flight == 0
    assert lender.runtime.closed == 1


def test_one_request_never_runs_past_the_ceiling(lender, quick, monkeypatch):
    monkeypatch.setattr(router, "REQUEST_CEILING_S", 0.35)
    lender.runtime.forever = True
    sent = asyncio.run(asyncio.wait_for(call_asgi(lender.app), 3))
    assert '"category":"link_ended"' in body_text(sent)
    assert lender.admission.in_flight == 0


def test_a_caller_that_hangs_up_gives_the_place_back(lender, quick):
    lender.runtime.forever = True

    async def go():
        hang_up = asyncio.Event()
        task = asyncio.ensure_future(call_asgi(lender.app, disconnect=hang_up))
        await asyncio.sleep(0.3)
        assert lender.admission.in_flight == 1
        hang_up.set()
        await asyncio.wait_for(task, 3)

    asyncio.run(go())
    assert lender.admission.in_flight == 0
    assert lender.runtime.closed == 1


def test_a_response_that_is_cancelled_before_it_starts_leaks_nothing(lender, quick):
    lender.runtime.forever = True

    async def go():
        gate = asyncio.Event()

        async def on_send(message):
            await gate.wait()

        task = asyncio.ensure_future(call_asgi(lender.app, on_send=on_send))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert lender.admission.in_flight == 0


def test_the_gate_does_not_read_the_body_of_a_caller_it_refuses(lender):
    async def go():
        reads = []

        async def receive():
            reads.append(1)
            return {"type": "http.request", "body": b"x", "more_body": True}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/circle/chat/completions", "raw_path": b"", "query_string": b"",
                 "headers": [], "client": (TAILSCALE, PORT), "server": (OWN, 8340)}
        await lender.app(scope, receive, send)
        return reads, sent

    reads, sent = asyncio.run(go())
    assert reads == [] and sent[0]["status"] == 403


def test_a_body_that_trickles_is_dropped_after_a_while_and_holds_no_place(lender, monkeypatch):
    monkeypatch.setattr(router, "BODY_TIMEOUT_S", 0.3)

    async def go():
        sent = []
        gate = asyncio.Event()

        async def receive():
            if not sent:
                sent.append(1)
                return {"type": "http.request", "body": b'{"messages": [', "more_body": True}
            await gate.wait()  # the rest never comes
            return {"type": "http.disconnect"}

        out = []

        async def send(message):
            out.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/circle/chat/completions", "raw_path": b"", "query_string": b"",
                 "headers": [(b"authorization", f"Bearer {TOKEN}".encode())], "client": (PEER, PORT),
                 "server": (OWN, 8340)}
        started = time.monotonic()
        await asyncio.wait_for(lender.app(scope, receive, send), 3)
        return out, time.monotonic() - started

    out, took = asyncio.run(go())
    assert out[0]["status"] == 408 and took < 1.5
    assert lender.admission.in_flight == 0 and lender.runtime.calls == []


def test_a_watcher_that_fails_cuts_the_stream_instead_of_leaving_it_running(lender, quick):
    lender.runtime.forever = True
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) > 3:
            raise RuntimeError("the policy check broke")
        return None

    lender.deps.sharing_refusal = flaky

    async def go():
        return await asyncio.wait_for(call_asgi(lender.app), 3)

    sent = asyncio.run(go())
    assert '"category":"link_ended"' in body_text(sent)
    assert lender.admission.in_flight == 0 and lender.runtime.closed == 1


def test_a_gpu_out_of_memory_chunk_reaches_the_circle_member_as_fixed_text(lender):
    class OutOfMemory(Runtime):
        async def stream_chat_raw(self, messages, **kw):
            yield {"choices": [{"delta": {"content": "Hel"}}]}
            yield {"__error__": "[WinError -529697949] Windows Error 0xe06d7363", "__category__": "gpu_out_of_memory"}

    lender.runtime = OutOfMemory()
    lender.deps.runtime = lambda: lender.runtime
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    frames = events(resp)
    assert frames[1] == {"error": {"category": "gpu_out_of_memory", "message": "The serving machine's GPU most likely ran out of memory."}}
    assert "WinError" not in resp.text and "0xe06d7363" not in resp.text and frames[-1] == "[DONE]"
    assert lender.admission.in_flight == 0
