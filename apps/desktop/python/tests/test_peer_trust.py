"""Who may call /peer/* and read the full /health body.

The engine listens on 0.0.0.0 and 100.64.0.0/10 is also what Tailscale and some
carrier and campus networks hand out, so "the source address is in the mesh
range" proves nothing. A non-loopback caller is a household member only when
this machine's own mesh sidecar lists its source address as a reachable peer,
the source is not this machine's own mesh address, and the connection was
accepted on this machine's own mesh address (services/peer_trust.py).

Starlette's TestClient sets the source with client=(ip, port) and the accepted
address through base_url. The `fake_mesh` fixture (conftest.py) stands in for
the sidecar.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services import peer_trust
from potluck_local.services.peer_trust import HouseholdRoster, Roster, decide, parse_ip, parse_roster

OWN = "100.64.0.4"
PEER = "100.64.0.9"
TAILSCALE = "100.101.102.103"  # inside 100.64.0.0/10, but not in this household
LAN = "192.168.1.50"
PORT = 40000


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    """The shared roster cache, driven by a clock the test moves by hand."""
    fake = Clock()
    monkeypatch.setattr(peer_trust, "_default", HouseholdRoster(clock=fake))
    return fake


@pytest.fixture()
def app():
    return create_app()


def client_for(app, source: str, accepted: str = OWN, **kwargs) -> TestClient:
    return TestClient(
        app, client=(source, PORT), base_url=f"http://{accepted}:8321", raise_server_exceptions=False, **kwargs
    )


def household(fake_mesh) -> None:
    fake_mesh.own_ip = OWN
    fake_mesh.add_peer(PEER)


# ---------------------------------------------------------------------------
# The middleware, end to end
# ---------------------------------------------------------------------------


def test_member_is_allowed_on_peer_capabilities(app, fake_mesh):
    household(fake_mesh)
    resp = client_for(app, PEER).get("/peer/capabilities")
    assert resp.status_code == 200
    assert "hostname" in resp.json()


def test_in_range_stranger_is_refused(app, fake_mesh):
    # A Tailscale-style address: inside 100.64.0.0/10, not in the roster.
    household(fake_mesh)
    resp = client_for(app, TAILSCALE).get("/peer/capabilities")
    assert resp.status_code == 403
    assert "household" in resp.json()["detail"]


def test_stranger_is_refused_with_the_same_body_whatever_the_reason(app, fake_mesh):
    # The caller learns nothing about why: not that the sidecar is down, not
    # that the address was right but the accepted address was wrong.
    household(fake_mesh)
    stranger = client_for(app, TAILSCALE).get("/peer/capabilities")
    wrong_address = client_for(app, PEER, accepted=TAILSCALE).get("/peer/capabilities")
    fake_mesh.down = True
    peer_trust.reset()
    down = client_for(app, PEER).get("/peer/capabilities")
    assert stranger.json() == wrong_address.json() == down.json()


def test_lan_address_is_refused_without_asking_the_sidecar(app, fake_mesh):
    household(fake_mesh)
    resp = client_for(app, LAN, accepted=LAN).get("/peer/capabilities")
    assert resp.status_code == 403
    assert fake_mesh.calls == []


def test_roster_entry_that_is_not_reachable_is_refused(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.add_peer(TAILSCALE, reachable=False)
    assert client_for(app, TAILSCALE).get("/peer/capabilities").status_code == 403


@pytest.mark.parametrize("reachable", ["true", 1, None, "yes", [True]])
def test_reachable_must_be_the_boolean_true(app, fake_mesh, reachable):
    household(fake_mesh)
    fake_mesh.add_peer(TAILSCALE, reachable=reachable)
    assert client_for(app, TAILSCALE).get("/peer/capabilities").status_code == 403


def test_this_machines_own_mesh_address_as_source_is_refused(app, fake_mesh):
    # A local process dialing the machine's own mesh address presents that
    # address as its source. It must not count as a peer, even if the roster
    # lists it.
    household(fake_mesh)
    fake_mesh.add_peer(OWN)
    resp = client_for(app, OWN, accepted=OWN).get("/peer/capabilities")
    assert resp.status_code == 403


def test_own_address_is_refused_without_a_token_dodge(monkeypatch, fake_mesh):
    # With the sidecar token set, loopback needs it on /peer/*. Dialing the
    # machine's own mesh address instead must not get around that.
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "secret")
    household(fake_mesh)
    app = create_app()
    assert client_for(app, "127.0.0.1", accepted="127.0.0.1").get("/peer/capabilities").status_code == 401
    assert client_for(app, OWN, accepted=OWN).get("/peer/capabilities").status_code == 403


@pytest.mark.parametrize("accepted", [TAILSCALE, "127.0.0.1", "192.168.1.20", "0.0.0.0", "testserver"])
def test_right_source_on_a_different_local_address_is_refused(app, fake_mesh, accepted):
    household(fake_mesh)
    assert client_for(app, PEER, accepted=accepted).get("/peer/capabilities").status_code == 403


def test_a_forged_host_header_does_not_change_the_accepted_address(app, fake_mesh):
    household(fake_mesh)
    wrong = client_for(app, PEER, accepted=TAILSCALE)
    resp = wrong.get("/peer/capabilities", headers={"Host": f"{OWN}:8321"})
    assert resp.status_code == 403
    right = client_for(app, PEER)
    resp = right.get("/peer/capabilities", headers={"Host": f"{TAILSCALE}:8321"})
    assert resp.status_code == 200


def test_forwarding_headers_do_not_change_the_source(app, fake_mesh):
    household(fake_mesh)
    resp = client_for(app, TAILSCALE).get(
        "/peer/capabilities",
        headers={"X-Forwarded-For": PEER, "X-Real-IP": PEER, "Forwarded": f"for={PEER}"},
    )
    assert resp.status_code == 403


def test_missing_server_address_is_refused(app, fake_mesh):
    household(fake_mesh)

    async def call(scope_server):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/peer/capabilities", "raw_path": b"/peer/capabilities",
            "query_string": b"", "headers": [(b"host", b"x")], "client": (PEER, PORT),
            "server": scope_server,
        }
        await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    for server in (None, ("/run/engine.sock", None), (), ("", 8321), (OWN,), (OWN, 8321, 0), "100.64.0.4", 7):
        assert asyncio.run(call(server)) == 403, server
    assert asyncio.run(call((OWN, 8321))) == 200
    assert asyncio.run(call([OWN, 8321])) == 200


def test_sidecar_unavailable_is_refused(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.down = True
    resp = client_for(app, PEER).get("/peer/capabilities")
    assert resp.status_code == 403


def test_a_missing_socket_is_refused(app, monkeypatch):
    # No fake: the real mesh_get runs and finds no socket (conftest points it at
    # a path that does not exist).
    resp = client_for(app, PEER).get("/peer/capabilities")
    assert resp.status_code == 403


JUNK_STATUS = [
    [],
    [OWN],
    "100.64.0.4",
    42,
    {},
    {"mesh_ip": None},
    {"mesh_ip": ""},
    {"mesh_ip": 1684275202},
    {"mesh_ip": True},
    {"mesh_ip": [OWN]},
    {"mesh_ip": {"ip": OWN}},
    {"mesh_ip": "not-an-address"},
    {"mesh_ip": "100.64.0.4/32"},
    {"mesh_ip": "100.64.0.4\n"},
    {"mesh_ip": " 100.64.0.4"},
    {"mesh_ip": "100.64.0.256"},
]

JUNK_PEERS = [
    [],
    [{"mesh_ip": PEER, "reachable": True}],
    "peers",
    None,
    {},
    {"peers": None},
    {"peers": {PEER: True}},
    {"peers": "x"},
    {"peers": [None, 3, "x", [PEER]]},
    {"peers": [{"reachable": True}]},
    {"peers": [{"mesh_ip": None, "reachable": True}]},
    {"peers": [{"mesh_ip": 1684275209, "reachable": True}]},
    {"peers": [{"mesh_ip": [PEER], "reachable": True}]},
    {"peers": [{"mesh_ip": "not-an-address", "reachable": True}]},
    {"peers": [{"mesh_ip": PEER}]},
    {"peers": [{"mesh_ip": PEER, "reachable": "true"}]},
]


@pytest.mark.parametrize("body", JUNK_STATUS, ids=repr)
def test_junk_status_is_refused_without_a_500(app, fake_mesh, body):
    household(fake_mesh)
    fake_mesh.status_body = body
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 403


@pytest.mark.parametrize("body", JUNK_PEERS, ids=repr)
def test_junk_peers_is_refused_without_a_500(app, fake_mesh, body):
    household(fake_mesh)
    fake_mesh.peers_body = body
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 403


def test_a_junk_entry_does_not_hide_a_good_one(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.peers_body = {"peers": [None, {"mesh_ip": 7, "reachable": True},
                                      {"mesh_ip": PEER, "reachable": True}]}
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 200


def test_ipv4_mapped_ipv6_addresses_are_matched_as_ipv4(app, fake_mesh):
    # A dual-stack listener reports an IPv4 peer as ::ffff:a.b.c.d.
    household(fake_mesh)

    async def call(source, accepted):
        transport = httpx.ASGITransport(app=app, client=(source, PORT))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://[{accepted}]:8321") as c:
            return (await c.get("/peer/capabilities")).status_code

    assert asyncio.run(call(f"::ffff:{PEER}", f"::ffff:{OWN}")) == 200
    assert asyncio.run(call(f"::ffff:{TAILSCALE}", f"::ffff:{OWN}")) == 403
    assert asyncio.run(call(f"::ffff:{PEER}", f"::ffff:{TAILSCALE}")) == 403


def test_peer_memory_search_is_refused_for_an_in_range_stranger(app, fake_mesh, monkeypatch):
    # Memory search is the endpoint that returns full memory content. With the
    # opt-in on, an address that is only "in the range" must still be refused
    # by the middleware, before the router runs.
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    household(fake_mesh)
    resp = client_for(app, TAILSCALE).post("/peer/memory/search", json={"query": "anything"})
    assert resp.status_code == 403
    assert "household" in resp.json()["detail"]
    assert "memories" not in resp.json()


def test_peer_memory_search_reaches_the_router_for_a_member(app, fake_mesh, monkeypatch):
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    household(fake_mesh)
    resp = client_for(app, PEER).post("/peer/memory/search", json={"query": "anything"})
    assert resp.status_code != 403 or "household" not in resp.json().get("detail", "")


def test_other_peer_routes_are_gated_too(app, fake_mesh):
    household(fake_mesh)
    stranger = client_for(app, TAILSCALE)
    for method, path in [("POST", "/peer/chat/completions"), ("POST", "/peer/memory/search"),
                         ("GET", "/peer/split/status"), ("GET", "/peer/anything")]:
        assert stranger.request(method, path, json={}).status_code == 403, path


def test_non_peer_paths_stay_forbidden_for_members(app, fake_mesh):
    household(fake_mesh)
    member = client_for(app, PEER)
    for path in ["/memory/projects", "/tasks", "/settings", "/health/inference", "/peer/../memory/projects"]:
        assert member.get(path).status_code == 403, path


# ---------------------------------------------------------------------------
# Peer scope: only the household counts
# ---------------------------------------------------------------------------

CIRCLE = "100.64.7.20"  # a machine of another household, listed by this machine's sidecar

# Anything but the exact string "household" means the entry is not the owner's
# machine. Includes values of the wrong type, which must not be coerced.
NOT_HOUSEHOLD = [
    "circle", "open", "pool", "Household", "HOUSEHOLD", " household", "household ",
    "household\n", "household\x00", "house-hold", "", None, 0, 1, 1.5, True, False,
    [], ["household"], {}, {"scope": "household"},
]


def test_scope_absent_is_allowed(app, fake_mesh):
    # A sidecar from before the tag existed lists no scope at all.
    household(fake_mesh)
    assert "scope" not in fake_mesh.peers[0]
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 200


def test_scope_household_is_allowed(app, fake_mesh):
    fake_mesh.own_ip = OWN
    fake_mesh.add_peer(PEER, scope="household")
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 200


def test_scope_circle_is_refused(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.add_peer(CIRCLE, scope="circle")
    assert client_for(app, CIRCLE).get("/peer/capabilities").status_code == 403


@pytest.mark.parametrize("scope", NOT_HOUSEHOLD, ids=repr)
def test_any_other_scope_is_refused(app, fake_mesh, scope):
    household(fake_mesh)
    fake_mesh.add_peer(CIRCLE, scope=scope)
    assert client_for(app, CIRCLE).get("/peer/capabilities").status_code == 403


@pytest.mark.parametrize("scope", NOT_HOUSEHOLD, ids=repr)
def test_parse_roster_ignores_any_scope_but_household(scope):
    status = {"mesh_ip": OWN}
    entry = {"mesh_ip": CIRCLE, "reachable": True, "scope": scope}
    roster = parse_roster(status, {"peers": [entry]})
    assert roster is not None and roster.peer_ips == frozenset()


def test_parse_roster_counts_absent_and_household_scope():
    status = {"mesh_ip": OWN}
    peers = {"peers": [
        {"mesh_ip": "100.64.0.9", "reachable": True},
        {"mesh_ip": "100.64.0.10", "reachable": True, "scope": "household"},
        {"mesh_ip": "100.64.0.11", "reachable": True, "scope": "circle"},
        {"mesh_ip": "100.64.0.12", "reachable": True, "scope": None},
        {"mesh_ip": "100.64.0.13", "reachable": False, "scope": "household"},
    ]}
    roster = parse_roster(status, peers)
    assert roster is not None
    assert roster.peer_ips == frozenset({parse_ip("100.64.0.9"), parse_ip("100.64.0.10")})


def test_a_circle_entry_does_not_hide_a_household_one_or_the_reverse(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.add_peer(CIRCLE, scope="circle")
    assert client_for(app, PEER).get("/peer/capabilities").status_code == 200
    assert client_for(app, CIRCLE).get("/peer/capabilities").status_code == 403


def test_a_circle_entry_cannot_reach_any_peer_route(app, fake_mesh, monkeypatch):
    # The middleware is the real one and the roster is read through the real
    # parser: only the sidecar is faked. Reachable, in range, accepted on this
    # machine's own mesh address, and still refused on every /peer/* route,
    # including memory search with the owner's opt-in on.
    monkeypatch.setenv("POTLUCK_PEER_MEMORY_ENABLED", "1")
    household(fake_mesh)
    fake_mesh.add_peer(CIRCLE, scope="circle")
    circle = client_for(app, CIRCLE)
    for method, path in [
        ("GET", "/peer/capabilities"), ("POST", "/peer/chat/completions"),
        ("POST", "/peer/memory/search"), ("GET", "/peer/split/status"), ("GET", "/peer/anything"),
    ]:
        resp = circle.request(method, path, json={"query": "anything"})
        assert resp.status_code == 403, path
        assert "household" in resp.json()["detail"], path
        assert "memories" not in resp.json(), path
    # Same refusal body as a stranger gets, so the scope is not revealed either.
    stranger = client_for(app, TAILSCALE).get("/peer/capabilities")
    assert circle.get("/peer/capabilities").json() == stranger.json()


def test_a_circle_entry_gets_only_the_minimal_health_body(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.add_peer(CIRCLE, scope="circle")
    assert client_for(app, CIRCLE).get("/health").json() == {"status": "ok"}
    assert "version" in client_for(app, PEER).get("/health").json()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_body_for_stranger_member_and_loopback(app, fake_mesh):
    household(fake_mesh)
    stranger = client_for(app, TAILSCALE).get("/health")
    lan = client_for(app, LAN, accepted=LAN).get("/health")
    member = client_for(app, PEER).get("/health")
    loopback = client_for(app, "127.0.0.1", accepted="127.0.0.1").get("/health")
    assert stranger.status_code == lan.status_code == 200
    assert stranger.json() == {"status": "ok"}
    assert lan.json() == {"status": "ok"}
    for full in (member, loopback):
        assert full.status_code == 200
        body = full.json()
        assert body["status"] == "ok"
        assert {"version", "model_loaded", "loaded_rpc_servers", "active_model_id"} <= set(body)


def test_health_for_a_member_on_the_wrong_address_is_minimal(app, fake_mesh):
    household(fake_mesh)
    assert client_for(app, PEER, accepted=TAILSCALE).get("/health").json() == {"status": "ok"}


def test_health_does_not_leak_other_machines_to_a_stranger(app, fake_mesh, monkeypatch):
    from potluck_local.routers import health

    class Runtime:
        def loaded_rpc_servers(self): return ["100.64.0.7:50052"]
        def __getattr__(self, name): return lambda *a, **k: None

    monkeypatch.setattr(health, "get_runtime", lambda: Runtime())
    household(fake_mesh)
    stranger = client_for(app, TAILSCALE).get("/health")
    assert stranger.text.replace(" ", "") == '{"status":"ok"}'
    assert "100.64.0.7" not in stranger.text
    assert "100.64.0.7" in client_for(app, PEER).get("/health").text


def test_health_with_the_sidecar_down_is_minimal_for_strangers_and_full_for_loopback(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.down = True
    assert client_for(app, PEER).get("/health").json() == {"status": "ok"}
    assert "version" in client_for(app, "127.0.0.1", accepted="127.0.0.1").get("/health").json()


def test_other_health_methods_are_unchanged(app, fake_mesh):
    household(fake_mesh)
    assert client_for(app, TAILSCALE).post("/health").status_code == 405


# ---------------------------------------------------------------------------
# Cache lifetime and read limit, through the middleware
# ---------------------------------------------------------------------------


def test_removed_member_is_refused_once_the_cache_lifetime_passes(app, fake_mesh, clock):
    household(fake_mesh)
    member = client_for(app, PEER)
    assert member.get("/peer/capabilities").status_code == 200
    fake_mesh.remove_peer(PEER)
    clock.advance(peer_trust.CACHE_TTL_S - 0.1)
    # Inside the lifetime the old answer may still be served.
    assert member.get("/peer/capabilities").status_code == 200
    clock.advance(0.1)
    assert member.get("/peer/capabilities").status_code == 403


def test_newly_added_member_is_allowed_without_waiting_for_the_cache(app, fake_mesh, clock):
    household(fake_mesh)
    assert client_for(app, TAILSCALE).get("/peer/capabilities").status_code == 403
    fake_mesh.add_peer(TAILSCALE)
    clock.advance(peer_trust.MIN_READ_INTERVAL_S)  # one second, well inside the 3 s cache
    assert client_for(app, TAILSCALE).get("/peer/capabilities").status_code == 200


def test_burst_of_strangers_reads_the_sidecar_once(app, fake_mesh, clock):
    household(fake_mesh)
    stranger = client_for(app, TAILSCALE)
    for _ in range(60):
        assert stranger.get("/peer/capabilities").status_code == 403
    assert fake_mesh.reads() == 1
    assert fake_mesh.calls.count("/peers") == 1


def test_concurrent_requests_share_one_refresh(app, fake_mesh):
    household(fake_mesh)
    fake_mesh.delay = 0.05

    async def run():
        transport = httpx.ASGITransport(app=app, client=(PEER, PORT))
        stranger = httpx.ASGITransport(app=app, client=(TAILSCALE, PORT))
        async with httpx.AsyncClient(transport=transport, base_url=fake_mesh.base_url) as m, \
                httpx.AsyncClient(transport=stranger, base_url=fake_mesh.base_url) as s:
            results = await asyncio.gather(
                *[m.get("/peer/capabilities") for _ in range(20)],
                *[s.get("/peer/capabilities") for _ in range(20)],
            )
        return [r.status_code for r in results]

    codes = asyncio.run(run())
    assert codes[:20] == [200] * 20
    assert codes[20:] == [403] * 20
    assert fake_mesh.reads() == 1


# ---------------------------------------------------------------------------
# HouseholdRoster on its own
# ---------------------------------------------------------------------------


class CountingRead:
    """A roster read that records when it ran."""

    def __init__(self, clock: Clock, roster: Roster | None) -> None:
        self.clock = clock
        self.roster = roster
        self.times: list[float] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self) -> Roster | None:
        self.times.append(self.clock())
        if self.gate is not None:
            await self.gate.wait()
        return self.roster


def roster_of(*peers: str, own: str = OWN) -> Roster:
    return Roster(own_ip=parse_ip(own), peer_ips=frozenset(parse_ip(p) for p in peers))


def test_roster_is_reused_within_the_cache_lifetime_then_read_again():
    async def run():
        clock = Clock()
        read = CountingRead(clock, roster_of(PEER))
        view = HouseholdRoster(read=read, clock=clock)
        for _ in range(5):
            assert await view.refusal_reason(PEER, OWN) is None
        assert read.times == [0.0]
        clock.advance(peer_trust.CACHE_TTL_S - 0.01)
        assert await view.refusal_reason(PEER, OWN) is None
        assert len(read.times) == 1
        clock.advance(0.01)
        assert await view.refusal_reason(PEER, OWN) is None
        assert len(read.times) == 2

    asyncio.run(run())


def test_new_member_inside_the_read_limit_is_refused_then_allowed():
    # Documents the one wait a joiner can see: the sidecar is never read twice
    # inside a second, so a joiner whose first request lands within a second of
    # the previous read is refused once and allowed on the next request after it.
    async def run():
        clock = Clock()
        read = CountingRead(clock, roster_of(PEER))
        view = HouseholdRoster(read=read, clock=clock)
        assert await view.refusal_reason(PEER, OWN) is None
        read.roster = roster_of(PEER, TAILSCALE)
        clock.advance(0.5)
        assert await view.refusal_reason(TAILSCALE, OWN) == "not_in_roster"
        assert len(read.times) == 1
        clock.advance(0.5)
        assert await view.refusal_reason(TAILSCALE, OWN) is None
        assert read.times == [0.0, 1.0]

    asyncio.run(run())


def test_strangers_never_make_the_sidecar_read_more_than_once_a_second():
    async def run():
        clock = Clock()
        read = CountingRead(clock, roster_of(PEER))
        view = HouseholdRoster(read=read, clock=clock)
        for _ in range(400):  # 40 simulated seconds, ten strangers per second
            assert await view.refusal_reason(TAILSCALE, OWN) == "not_in_roster"
            clock.advance(0.1)
        gaps = [b - a for a, b in zip(read.times, read.times[1:])]
        assert gaps and min(gaps) >= peer_trust.MIN_READ_INTERVAL_S - 1e-9
        assert len(read.times) <= 41

    asyncio.run(run())


def test_a_stranger_flood_on_an_unavailable_sidecar_is_also_limited():
    async def run():
        clock = Clock()
        read = CountingRead(clock, None)
        view = HouseholdRoster(read=read, clock=clock)
        for _ in range(200):
            assert await view.refusal_reason(PEER, OWN) == "roster_unavailable"
        assert read.times == [0.0]
        clock.advance(1.0)
        assert await view.refusal_reason(PEER, OWN) == "roster_unavailable"
        assert read.times == [0.0, 1.0]

    asyncio.run(run())


def test_the_roster_recovers_when_the_sidecar_comes_back():
    async def run():
        clock = Clock()
        read = CountingRead(clock, None)
        view = HouseholdRoster(read=read, clock=clock)
        assert await view.refusal_reason(PEER, OWN) == "roster_unavailable"
        read.roster = roster_of(PEER)
        clock.advance(1.0)
        assert await view.refusal_reason(PEER, OWN) is None

    asyncio.run(run())


def test_concurrent_callers_share_one_read_and_all_get_the_answer():
    async def run():
        clock = Clock()
        read = CountingRead(clock, roster_of(PEER))
        read.gate = asyncio.Event()
        view = HouseholdRoster(read=read, clock=clock)
        tasks = [asyncio.ensure_future(view.refusal_reason(PEER, OWN)) for _ in range(25)]
        tasks += [asyncio.ensure_future(view.refusal_reason(TAILSCALE, OWN)) for _ in range(25)]
        await asyncio.sleep(0.01)
        assert len(read.times) == 1
        read.gate.set()
        results = await asyncio.gather(*tasks)
        assert results[:25] == [None] * 25
        assert results[25:] == ["not_in_roster"] * 25
        assert len(read.times) == 1

    asyncio.run(run())


def test_a_caller_that_goes_away_does_not_cancel_the_shared_read():
    async def run():
        clock = Clock()
        read = CountingRead(clock, roster_of(PEER))
        read.gate = asyncio.Event()
        view = HouseholdRoster(read=read, clock=clock)
        first = asyncio.ensure_future(view.refusal_reason(PEER, OWN))
        second = asyncio.ensure_future(view.refusal_reason(PEER, OWN))
        await asyncio.sleep(0.01)
        first.cancel()
        read.gate.set()
        assert await second is None
        assert first.cancelled()
        assert len(read.times) == 1

    asyncio.run(run())


def test_a_read_that_raises_means_unavailable():
    async def run():
        async def boom():
            raise RuntimeError("socket exploded")

        view = HouseholdRoster(read=boom, clock=Clock())
        assert await view.refusal_reason(PEER, OWN) == "roster_unavailable"

    asyncio.run(run())


def test_an_unexpected_failure_means_not_a_member():
    class BrokenClock:
        def __call__(self):
            raise RuntimeError("clock exploded")

    view = HouseholdRoster(read=CountingRead(Clock(), roster_of(PEER)), clock=BrokenClock())
    assert asyncio.run(view.refusal_reason(PEER, OWN)) == "check_failed"


def test_one_roster_serves_successive_event_loops():
    clock = Clock()
    read = CountingRead(clock, roster_of(PEER))
    view = HouseholdRoster(read=read, clock=clock)
    for _ in range(3):
        assert asyncio.run(view.refusal_reason(PEER, OWN)) is None
        clock.advance(peer_trust.CACHE_TTL_S)


# ---------------------------------------------------------------------------
# The pure decision
# ---------------------------------------------------------------------------


def test_decision_reasons():
    roster = roster_of(PEER)
    assert decide(roster, PEER, OWN) is None
    assert decide(None, PEER, OWN) == "roster_unavailable"
    assert decide(roster, "not-an-ip", OWN) == "bad_source"
    assert decide(roster, None, OWN) == "bad_source"
    assert decide(roster, OWN, OWN) == "own_address"
    assert decide(roster, TAILSCALE, OWN) == "not_in_roster"
    assert decide(roster, PEER, TAILSCALE) == "wrong_local_address"
    assert decide(roster, PEER, None) == "wrong_local_address"
    assert decide(roster, PEER, 5) == "wrong_local_address"


def test_the_roster_never_lists_this_machine_as_a_peer():
    roster = parse_roster({"mesh_ip": OWN}, {"peers": [{"mesh_ip": OWN, "reachable": True},
                                                       {"mesh_ip": PEER, "reachable": True}]})
    assert roster is not None and parse_ip(OWN) not in roster.peer_ips


def test_parse_ip_takes_only_strings():
    assert parse_ip(PEER) == parse_ip(f"::ffff:{PEER}")
    for value in (None, 0, 1684275209, True, b"100.64.0.9", ["100.64.0.9"], "100.64.0.9\n", ""):
        assert parse_ip(value) is None, value


def test_the_shared_roster_can_be_reset():
    first = peer_trust.household_roster()
    assert peer_trust.household_roster() is first
    peer_trust.reset()
    assert peer_trust.household_roster() is not first
