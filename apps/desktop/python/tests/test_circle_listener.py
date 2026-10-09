"""The circle listener's life: when it runs, what it attests, how it is bound.

Three layers. The listener itself (real sockets). The contributor service that
decides when it runs (a stand-in listener and a stand-in sidecar, so each of the
three conditions can be flipped alone). And the real contributor loop against a
fake coordinator, to see what goes on the wire: no circle fields at all for a
coordinator that does not broker links, and a re-attestation when the listener
comes up.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import ipaddress
import json
import signal
import socket
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
import websockets
from circle_support import FIXTURE, MODEL, NODE
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potluck_local.contributor import client as contributor
from potluck_local.contributor.config import Settings
from potluck_local.contributor.schemas import AcceptMessage, AttestationPayload, JobAssignedMessage
from potluck_local.contributor.telemetry import Capacity
from potluck_local.services import circle_listener, circle_mesh
from potluck_local.services import contributor_service as cs
from potluck_local.services.circle_listener import CircleListener, ListenerError
from potluck_local.services.settings_manager import NetworkSettings, PotluckSettings


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


async def until(predicate, timeout: float = 3.0, step: float = 0.01) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not reached")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def hello_app(scope, receive, send):
    """An ASGI app that answers 200 to everything."""
    if scope["type"] == "lifespan":
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


# ---- the listener on real sockets ---------------------------------------------


@run_async
async def test_the_listener_binds_serves_and_stops():
    port = free_port()
    listener = CircleListener(hello_app, host="127.0.0.1", port=port)
    assert listener.port is None and not listener.running
    assert await listener.start() == port
    assert listener.running and listener.port == port
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"http://127.0.0.1:{port}/anything")).text == "ok"
    await listener.stop()
    assert not listener.running and listener.port is None
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


@run_async
async def test_a_busy_port_moves_to_the_next_free_one_and_reports_it():
    port = free_port()
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", port))
    squatter.listen()
    try:
        listener = CircleListener(hello_app, host="127.0.0.1", port=port)
        bound = await listener.start()
        assert bound != port and port < bound < port + circle_listener.PORT_TRIES
        assert listener.port == bound
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{bound}/")).status_code == 200
        await listener.stop()
    finally:
        squatter.close()


@run_async
async def test_several_busy_ports_in_a_row_are_skipped():
    port = free_port()
    squatters = []
    try:
        for offset in range(3):
            s = socket.socket()
            s.bind(("127.0.0.1", port + offset))
            s.listen()
            squatters.append(s)
        listener = CircleListener(hello_app, host="127.0.0.1", port=port)
        assert await listener.start() == port + 3
        await listener.stop()
    except OSError:
        pytest.skip("a neighbouring port was in use")
    finally:
        for s in squatters:
            s.close()


def test_no_free_port_is_a_listener_error():
    port = free_port()
    squatters = []
    try:
        for offset in range(2):
            s = socket.socket()
            s.bind(("127.0.0.1", port + offset))
            s.listen()
            squatters.append(s)
        with pytest.raises(ListenerError):
            circle_listener.bind_listening_socket("127.0.0.1", port, tries=2)
    finally:
        for s in squatters:
            s.close()


def test_an_address_that_cannot_be_bound_is_a_listener_error():
    with pytest.raises(ListenerError):
        circle_listener.bind_listening_socket("203.0.113.77", 9, tries=2)  # not an address of this machine


@run_async
async def test_a_listener_error_leaves_nothing_running_and_can_be_retried():
    listener = CircleListener(hello_app, host="203.0.113.77", port=free_port())
    with pytest.raises(ListenerError):
        await listener.start()
    assert not listener.running
    await listener.stop()  # safe when it never ran
    listener = CircleListener(hello_app, host="127.0.0.1", port=free_port())
    await listener.start()
    await listener.stop()


@run_async
async def test_the_listener_does_not_take_over_the_processs_signal_handlers():
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    listener = CircleListener(hello_app, host="127.0.0.1", port=free_port())
    await listener.start()
    assert {sig: signal.getsignal(sig) for sig in before} == before
    await listener.stop()
    assert {sig: signal.getsignal(sig) for sig in before} == before


@run_async
async def test_starting_twice_is_the_same_listener():
    listener = CircleListener(hello_app, host="127.0.0.1", port=free_port())
    first = await listener.start()
    assert await listener.start() == first
    await listener.stop()


@run_async
async def test_stopping_cuts_a_request_that_is_still_streaming(monkeypatch):
    monkeypatch.setattr(circle_listener, "GRACEFUL_SHUTDOWN_S", 1)

    async def slow_app(scope, receive, send):
        if scope["type"] == "lifespan":
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await asyncio.sleep(30)

    listener = CircleListener(slow_app, host="127.0.0.1", port=free_port())
    port = await listener.start()

    async def fetch():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/") as resp:
                async for _ in resp.aiter_bytes():
                    pass

    task = asyncio.ensure_future(fetch())
    await asyncio.sleep(0.3)
    started = asyncio.get_event_loop().time()
    await asyncio.wait_for(listener.stop(), 8)
    assert asyncio.get_event_loop().time() - started < 6
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@run_async
async def test_the_real_app_behind_a_real_socket_refuses_a_stranger_and_has_no_household_routes(fake_mesh):
    from circle_support import NODE as _node
    from test_circle_gate import Lender

    lender = Lender(fake_mesh)
    listener = CircleListener(lender.app, host="127.0.0.1", port=free_port())
    port = await listener.start()
    try:
        async with httpx.AsyncClient() as client:
            base = f"http://127.0.0.1:{port}"
            # Dialed on 127.0.0.1, not on this machine's mesh address: refused whatever it carries.
            assert (await client.get(f"{base}/circle/capabilities")).status_code == 403
            forged = {"X-Forwarded-For": "100.64.1.2", "Forwarded": "for=100.64.1.2", "X-Real-IP": "100.64.1.2"}
            assert (await client.get(f"{base}/circle/capabilities", headers=forged)).status_code == 403
            for path in ("/peer/capabilities", "/health", "/v1/models", "/memory/search", "/docs"):
                assert (await client.get(f"{base}{path}")).status_code == 404, path
    finally:
        await listener.stop()
    assert _node


def local_lan_address() -> str | None:
    """An IPv4 address of this machine other than loopback, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing address
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::"])
def test_the_listener_refuses_to_be_told_to_bind_every_interface(host):
    with pytest.raises(ValueError):
        CircleListener(hello_app, host=host)


@run_async
async def test_a_connection_to_another_address_of_this_machine_is_refused_at_connect_not_by_the_gate():
    lan = local_lan_address()
    if lan is None:
        pytest.skip("this machine has no address besides loopback")
    port = free_port()
    listener = CircleListener(hello_app, host="127.0.0.1", port=port)
    assert await listener.start() == port
    try:
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{port}/")).status_code == 200
        with pytest.raises(OSError):  # (a connect to 0.0.0.0 would mean loopback on most systems, so not tried)
            socket.create_connection((lan, port), timeout=2).close()
    finally:
        await listener.stop()


@run_async
async def test_a_listener_bound_to_a_lan_address_cannot_be_reached_over_loopback():
    lan = local_lan_address()
    if lan is None:
        pytest.skip("this machine has no address besides loopback")
    port = free_port()
    listener = CircleListener(hello_app, host=lan, port=port)
    assert await listener.start() == port
    try:
        assert listener.host == lan
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://{lan}:{port}/")).status_code == 200
    finally:
        await listener.stop()


def test_an_address_this_machine_does_not_carry_is_one_failure_not_thirty_two():
    attempts = []
    real = socket.socket

    class Counting(real):
        def bind(self, address):
            attempts.append(address)
            return super().bind(address)

    import unittest.mock as mock

    with mock.patch.object(socket, "socket", Counting), pytest.raises(circle_listener.AddressNotAvailable):
        circle_listener.bind_listening_socket("203.0.113.77", 18200)
    assert len(attempts) == 1


def test_the_port_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("POTLUCK_CIRCLE_PORT", raising=False)
    assert circle_listener.wanted_port() == 8340
    monkeypatch.setenv("POTLUCK_CIRCLE_PORT", "18493")
    assert circle_listener.wanted_port() == 18493
    for junk in ("", "abc", "0", "70000", "-5", "8340.5"):
        monkeypatch.setenv("POTLUCK_CIRCLE_PORT", junk)
        assert circle_listener.wanted_port() == 8340, junk


@pytest.mark.parametrize("host,loopback", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.5.5.5", True),
    ("0.0.0.0", False), ("", False), ("192.168.1.2", False), ("100.64.0.4", False), ("::", False),
])
def test_a_loopback_bind_address_is_recognised(host, loopback):
    assert circle_listener.host_is_loopback(host) is loopback


def test_the_bind_address_follows_the_main_servers_setting(monkeypatch):
    monkeypatch.delenv("POTLUCK_BIND_HOST", raising=False)
    assert circle_listener.bind_host() == "0.0.0.0"
    monkeypatch.setenv("POTLUCK_BIND_HOST", "127.0.0.1")
    assert circle_listener.bind_host() == "127.0.0.1"


# ---- the service decides when it runs ---------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", raising=False)
    monkeypatch.delenv("POTLUCK_BIND_HOST", raising=False)
    monkeypatch.setattr(cs, "CIRCLE_CHECK_INTERVAL_S", 0.03)


class FakeLoopRunner:
    def __init__(self):
        self.starts = 0

    async def __call__(self, settings, priv, hooks, stop):
        self.starts += 1
        await stop.wait()
        hooks.on_state("stopped")


class FakeRuntime:
    model_id = MODEL

    def is_loaded(self):
        return True

    def current_model_id(self):
        return self.model_id

    def benchmarks(self):
        return {}


MESH_IP = "100.64.0.4"


class FakeListener:
    def __init__(self, port: int = 8347, fail: Exception | None = None, host: str = MESH_IP) -> None:
        self.port_ = port
        self.fail = fail
        self._host = host
        self.starts = 0
        self.stops = 0
        self._running = False

    async def start(self) -> int:
        self.starts += 1
        if self.fail is not None:
            raise self.fail
        self._running = True
        return self.port_

    async def stop(self) -> None:
        self._running = False
        self.stops += 1

    @property
    def running(self) -> bool:
        return self._running

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int | None:
        return self.port_ if self._running else None


@dataclass
class Rig:
    svc: cs.ContributorService
    listeners: list[FakeListener] = field(default_factory=list)
    sidecar: dict = field(default_factory=dict)
    reads: int = 0
    next_listener: FakeListener | None = None

    def up(self) -> FakeListener | None:
        return next((l for l in self.listeners if l.running), None)


def status_with(node=NODE, mesh_ip=MESH_IP, features=frozenset({circle_mesh.LINK_FEATURE})):
    return circle_mesh.SidecarStatus(node, ipaddress.ip_address(mesh_ip) if mesh_ip else None, features)


def make_rig(monkeypatch, tmp_path, *, runner=None, next_listener=None) -> Rig:
    from potluck_local.contributor.config import Settings as ContributorSettings
    from potluck_local.services import pool_routing

    monkeypatch.setenv("POTLUCK_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("POTLUCK_BINARY_HASH", raising=False)
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.setattr(cs, "get_contributor_settings",
                        lambda: ContributorSettings(_env_file=None, key_dir=tmp_path / "keys"))
    monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: "http://10.0.0.1:8400")

    async def hardware():
        return {"gpu_name": "FakeGPU"}

    rig = Rig(svc=None)  # type: ignore[arg-type]
    rig.sidecar = {"status": status_with()}

    async def reader():
        rig.reads += 1
        return rig.sidecar["status"]

    def factory(service, host):
        listener = rig.next_listener or FakeListener()
        rig.next_listener = None
        listener._host = host
        rig.listeners.append(listener)
        return listener

    rig.next_listener = next_listener
    rig.svc = cs.ContributorService(
        runtime_getter=FakeRuntime, loop_runner=runner or FakeLoopRunner(), hardware_getter=hardware,
        circle_listener_factory=factory, circle_status_reader=reader)
    return rig


def settings(audience="circles", *, direct=True, cap=2, on=True) -> PotluckSettings:
    return PotluckSettings(network=NetworkSettings(
        contributor_mode=on, coordinator_url="http://10.0.0.1:8400", max_concurrent_jobs=cap,
        contributor_audience=audience, direct_circle_links=direct))


def accept(svc, *, links=True, attested=False, bound=False):
    svc._on_state("connected", peer_id="p", session_id="s", heartbeat_interval_s=30,
                  circle_links=links, circle_attested=attested, circle_bound=bound)


@run_async
async def test_the_listener_comes_up_before_any_accept_once_its_local_conditions_hold(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    await until(lambda: rig.svc.circle_attest() is not None)
    assert rig.svc.circle_attest() == (NODE, 8347), "ready for the first attestation, no accept needed"
    assert rig.svc.status()["circle_port"] == 8347
    assert rig.up() is rig.listeners[0] and len(rig.listeners) == 1
    assert rig.listeners[0].host == MESH_IP, "bound to the mesh address the sidecar reported"
    await rig.svc.stop()
    assert rig.up() is None and rig.svc.circle_attest() is None and rig.svc.status()["circle_port"] is None


@pytest.mark.parametrize("links", [None, False, "true", 1])
@run_async
async def test_an_accept_that_does_not_say_links_are_brokered_stops_the_listener_and_keeps_it_stopped(
        monkeypatch, tmp_path, links):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    await until(lambda: rig.up() is not None)
    rig.svc._on_state("connected", peer_id="p", circle_links=links, circle_attested=True, circle_bound=False)
    await until(lambda: rig.up() is None and rig.svc.circle_attest() is None)
    for _ in range(3):  # reconnects of the same coordinator: no fields, no listener
        rig.svc._on_state("disconnected", error="x")
        rig.svc._on_state("connected", peer_id="p", circle_links=links)
        await asyncio.sleep(0.12)
    assert len(rig.listeners) == 1 and rig.up() is None
    # A later accept that does say so brings it back (one attestation will carry it).
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    assert len(rig.listeners) == 2
    await rig.svc.stop()


@run_async
async def test_a_machine_that_shares_with_nobody_but_its_owner_has_no_listener(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings(audience="mine"))
    accept(rig.svc)
    await asyncio.sleep(0.2)
    assert rig.listeners == [] and not rig.svc.is_running


@run_async
async def test_contribute_off_means_no_listener(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings(on=False))
    accept(rig.svc)
    await asyncio.sleep(0.2)
    assert rig.listeners == []


@run_async
async def test_everyone_includes_circles(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings(audience="everyone"))
    accept(rig.svc)
    await until(lambda: rig.svc.circle_attest() is not None)
    await rig.svc.stop()


@run_async
async def test_a_sidecar_without_the_feature_means_no_listener(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    rig.sidecar["status"] = circle_mesh.SidecarStatus(NODE, None, frozenset())
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await asyncio.sleep(0.3)
    assert rig.listeners == [] and rig.reads > 0
    await rig.svc.stop()


@pytest.mark.parametrize("status", [
    None,
    circle_mesh.SidecarStatus(None, None, frozenset({circle_mesh.LINK_FEATURE})),
], ids=["unreadable", "no_node_id"])
@run_async
async def test_a_sidecar_that_cannot_be_read_or_has_no_node_means_no_listener(monkeypatch, tmp_path, status):
    rig = make_rig(monkeypatch, tmp_path)
    rig.sidecar["status"] = status
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await asyncio.sleep(0.3)
    assert rig.listeners == []
    await rig.svc.stop()


@run_async
async def test_the_listener_stops_when_each_condition_stops_holding(monkeypatch, tmp_path):
    async def scenario(break_it):
        rig = make_rig(monkeypatch, tmp_path)
        await rig.svc.apply_settings(settings())
        accept(rig.svc)
        await until(lambda: rig.up() is not None)
        await break_it(rig)
        await until(lambda: rig.up() is None and rig.svc.circle_attest() is None)
        assert rig.listeners[0].stops == 1
        await rig.svc.stop()

    async def accept_withdrawn(rig):  # the next accept says links are off
        accept(rig.svc, links=None)

    async def sharing_narrowed(rig):
        await rig.svc.apply_settings(settings(audience="mine"))

    async def direct_links_off(rig):
        await rig.svc.apply_settings(settings(direct=False))

    async def contribute_off(rig):
        await rig.svc.apply_settings(settings(on=False))

    async def sidecar_loses_the_feature(rig):
        rig.sidecar["status"] = circle_mesh.SidecarStatus(NODE, None, frozenset())

    async def sidecar_gone(rig):
        rig.sidecar["status"] = None

    async def coordinator_rejects(rig):
        rig.svc._on_state("rejected", reason="unauthorized")

    for break_it in (accept_withdrawn, sharing_narrowed, direct_links_off, contribute_off,
                     sidecar_loses_the_feature, sidecar_gone, coordinator_rejects):
        await scenario(break_it)


@run_async
async def test_a_sidecar_that_answers_without_the_feature_takes_the_listener_down_at_once(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "CIRCLE_CHECK_INTERVAL_S", 30.0)  # only the wake-up from a change can act
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    rig.sidecar["status"] = circle_mesh.SidecarStatus(NODE, None, frozenset())
    rig.svc._poke_circle()
    await until(lambda: rig.up() is None, timeout=1.0)
    await rig.svc.stop()


@pytest.mark.parametrize("mesh_ip", [None, "0.0.0.0", "224.0.0.1", "8.8.8.8", "192.168.1.5", "::1", "100.64.0.4/32"],
                         ids=["unknown", "unspecified", "multicast", "outside_mesh", "lan", "ipv6", "junk"])
@run_async
async def test_no_listener_without_a_usable_mesh_address_and_never_a_wildcard(monkeypatch, tmp_path, mesh_ip):
    rig = make_rig(monkeypatch, tmp_path)
    try:
        rig.sidecar["status"] = status_with(mesh_ip=mesh_ip)
    except ValueError:
        rig.sidecar["status"] = status_with(mesh_ip=None)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await asyncio.sleep(0.25)
    assert rig.listeners == [] and rig.svc.circle_attest() is None
    await rig.svc.stop()


@run_async
async def test_the_listener_follows_the_mesh_address_when_it_changes(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    await until(lambda: rig.up() is not None)
    first = rig.listeners[0]
    rig.sidecar["status"] = status_with(mesh_ip="100.64.7.9")
    rig.svc._poke_circle()
    await until(lambda: rig.up() is not None and rig.up() is not first)
    assert first.stops == 1 and rig.up().host == "100.64.7.9" and len(rig.listeners) == 2
    await rig.svc.stop()


@run_async
async def test_an_address_the_machine_does_not_carry_yet_is_asked_again_at_the_next_check(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "CIRCLE_RETRY_S", 60.0)  # an ordinary failure would wait this long
    rig = make_rig(monkeypatch, tmp_path,
                   next_listener=FakeListener(fail=circle_listener.AddressNotAvailable("not here yet")))
    await rig.svc.apply_settings(settings())
    await until(lambda: len(rig.listeners) == 1)
    await until(lambda: rig.up() is not None, timeout=2)  # the second try, a check later, binds
    assert len(rig.listeners) == 2
    await rig.svc.stop()


@run_async
async def test_a_blip_of_the_sidecar_does_not_take_the_listener_down(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    good = rig.sidecar["status"]
    rig.sidecar["status"] = None
    await asyncio.sleep(0.05)  # one or two bad checks, fewer than the strikes
    rig.sidecar["status"] = good
    await asyncio.sleep(0.3)
    assert rig.up() is not None and rig.listeners[0].stops == 0
    await rig.svc.stop()


@run_async
async def test_a_listener_that_stays_up_across_a_reconnect_is_not_restarted(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    rig.svc._on_state("disconnected", error="x")
    rig.svc._on_state("connecting")
    accept(rig.svc, attested=True, bound=True)
    await asyncio.sleep(0.2)
    assert len(rig.listeners) == 1 and rig.listeners[0].starts == 1 and rig.listeners[0].stops == 0
    await rig.svc.stop()


@run_async
async def test_a_coordinator_that_will_not_bind_the_node_stops_the_listener_and_does_not_flap(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    accept(rig.svc, attested=True, bound=False)  # attested the node, accept did not echo it
    await until(lambda: rig.up() is None)
    for _ in range(3):  # reconnects without fields: still no listener
        accept(rig.svc, attested=False, bound=False)
        await asyncio.sleep(0.15)
    assert len(rig.listeners) == 1 and rig.svc.circle_attest() is None
    # A new sign-in may be bound where the old one was not.
    rig.svc.set_member_session("sess-new")
    await until(lambda: rig.up() is not None)
    assert len(rig.listeners) == 2
    await rig.svc.stop()


@run_async
async def test_a_bound_accept_clears_a_refusal(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    rig.svc._circle_bind_refused = True
    accept(rig.svc, attested=True, bound=True)
    await until(lambda: rig.up() is not None)
    await rig.svc.stop()


@run_async
async def test_the_node_is_re_attested_when_the_sidecar_enrolls_again(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.svc.circle_attest() == (NODE, 8347))
    other = str(uuid.uuid4())
    rig.sidecar["status"] = status_with(node=other)
    await until(lambda: rig.svc.circle_attest() == (other, 8347))
    assert len(rig.listeners) == 1
    await rig.svc.stop()


@run_async
async def test_a_failure_to_bind_never_reaches_the_loop_and_is_retried_later(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "CIRCLE_RETRY_S", 0.3)
    runner = FakeLoopRunner()
    rig = make_rig(monkeypatch, tmp_path, runner=runner, next_listener=FakeListener(fail=ListenerError("no free port")))
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: len(rig.listeners) == 1)
    await asyncio.sleep(0.15)
    assert len(rig.listeners) == 1, "not retried inside the retry window"
    assert rig.svc.is_running and rig.svc.status()["state"] == "connected" and runner.starts == 1
    assert rig.svc.status()["last_error"] is None and rig.svc.circle_attest() is None
    await until(lambda: rig.up() is not None, timeout=2)
    assert len(rig.listeners) == 2 and runner.starts == 1
    await rig.svc.stop()


@pytest.mark.parametrize("error", [RuntimeError("boom"), OSError("nope"), ValueError("x"), TimeoutError()])
@run_async
async def test_any_error_starting_the_listener_is_contained(monkeypatch, tmp_path, error):
    monkeypatch.setattr(cs, "CIRCLE_RETRY_S", 60.0)
    rig = make_rig(monkeypatch, tmp_path, next_listener=FakeListener(fail=error))
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: len(rig.listeners) == 1)
    await asyncio.sleep(0.2)
    assert rig.svc.is_running and rig.svc.circle_attest() is None and len(rig.listeners) == 1
    await rig.svc.stop()


@run_async
async def test_a_listener_that_dies_is_started_again(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    rig.listeners[0]._running = False  # its server task ended on its own
    await until(lambda: rig.up() is not None and rig.up() is not rig.listeners[0])
    assert len(rig.listeners) == 2
    await rig.svc.stop()


@run_async
async def test_a_busy_default_port_is_reported_as_whatever_was_bound(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path, next_listener=FakeListener(port=8343))
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.svc.circle_attest() is not None)
    assert rig.svc.circle_attest() == (NODE, 8343) and rig.svc.status()["circle_port"] == 8343
    await rig.svc.stop()


@run_async
async def test_a_main_server_bound_to_loopback_serves_no_one_else(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_BIND_HOST", "127.0.0.1")
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await asyncio.sleep(0.2)
    assert rig.listeners == []
    await rig.svc.stop()


@run_async
async def test_stopping_the_service_stops_the_watcher_and_the_listener(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings())
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    watcher = rig.svc._circle_task
    await rig.svc.stop()
    assert watcher.done() and rig.svc._circle_task is None and rig.up() is None


@run_async
async def test_a_restart_for_a_new_setting_takes_the_listener_down_with_the_loop(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    await rig.svc.apply_settings(settings(cap=1))
    accept(rig.svc)
    await until(lambda: rig.up() is not None)
    await rig.svc.apply_settings(settings(cap=3))  # a new cap restarts the loop
    assert rig.listeners[0].stops == 1 and rig.svc.circle_attest() is None
    accept(rig.svc)  # the new session's accept
    await until(lambda: rig.svc.circle_attest() is not None)
    assert len(rig.listeners) == 2
    await rig.svc.stop()


def test_the_gate_reads_the_owners_sharing_choice_live(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    svc = rig.svc
    assert svc.circle_sharing_refusal() == "not_configured"
    svc._network = settings().network
    assert svc.circle_sharing_refusal() is None
    svc._network = settings(direct=False).network
    assert svc.circle_sharing_refusal() == "links_off"
    svc._network = settings(audience="mine").network
    assert svc.circle_sharing_refusal() == "not_sharing_circles"
    svc._network = settings(on=False).network
    assert svc.circle_sharing_refusal() is not None
    svc._network = settings(cap=0).network
    assert svc.circle_sharing_refusal() == "job_limit_zero"
    svc._network = settings(audience=None).network
    assert svc.circle_sharing_refusal() == "not_sharing_circles"


def test_relay_and_direct_jobs_count_against_one_cap(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    svc = rig.svc
    svc._network = settings(cap=2).network
    svc._on_state("job_started", job_id="a")
    first = svc._admission.try_acquire()
    assert first is not None and svc._admission.try_acquire() is None, "one relay + one direct = the cap of 2"
    svc._on_state("job_finished", job_id="a")
    second = svc._admission.try_acquire()
    assert second is not None
    svc._on_state("job_started", job_id="b")
    assert svc._admission.snapshot() == (3, 2), "a relay job already past the cap changes nothing"
    assert svc._admission.try_acquire() is None
    first.release()
    second.release()
    svc._on_state("disconnected", error="x")
    assert svc._relay_in_flight == 0


# ---- what goes on the wire -------------------------------------------------------------


BASE_ATTEST_KEYS = {"type", "member_id", "peer_public_key", "binary_hash", "client_version", "platform", "hostname",
                    "hardware_summary", "nonce_signature", "capabilities", "audiences"}


@dataclass
class Coordinator:
    """A coordinator for the contributor loop: records attestations, answers accepts."""

    circle_links: bool | None = None  # what the accept says; None leaves the key out
    echo_binding: bool = True  # echoes mesh_node_id when the attestation named one
    heartbeat_interval_s: int = 30
    attestations: list[dict] = field(default_factory=list)
    accepts: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    connections: int = 0
    _server: object = None
    url: str = ""
    _conn: object = None
    _connected: asyncio.Event = field(default_factory=asyncio.Event)

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/peers/connect"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        self.connections += 1
        nonce = base64.urlsafe_b64encode(b"n" * 32).rstrip(b"=").decode()
        await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
        attest = json.loads(await ws.recv())
        self.attestations.append(attest)
        accept = {"type": "accept", "peer_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
                  "server_time": "2026-10-09T00:00:00Z", "heartbeat_interval_s": self.heartbeat_interval_s}
        if attest.get("audiences") is not None:
            accept["audiences"] = [a for a in ("circle", "pool") if a in attest["audiences"]]
        if self.circle_links is not None:
            accept["circle_links"] = self.circle_links
            if self.echo_binding and attest.get("mesh_node_id"):
                accept["mesh_node_id"] = attest["mesh_node_id"]
        self.accepts.append(accept)
        await ws.send(json.dumps(accept))
        self._conn = ws
        self._connected.set()
        try:
            async for raw in ws:
                self.frames.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._connected.clear()

    async def drop(self) -> None:
        """Close the connection from the coordinator's side."""
        await self._conn.close()

    async def assign(self, slug=MODEL):
        job_id = uuid.uuid4()
        await self._conn.send(json.dumps({"type": "job_assigned", "job_id": str(job_id), "model_slug": slug,
                                          "prompt": "hi", "sampling": {"max_tokens": 8}}))
        return job_id


async def run_service(monkeypatch, tmp_path, coordinator, *, audience="circles", direct=True):
    from potluck_local.contributor.config import Settings as ContributorSettings
    from potluck_local.services import pool_routing

    monkeypatch.setattr(contributor, "CAPABILITY_CHECK_INTERVAL_S", 0.05)
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 0.05)
    monkeypatch.setenv("POTLUCK_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("POTLUCK_BINARY_HASH", raising=False)
    monkeypatch.setattr(cs, "get_contributor_settings",
                        lambda: ContributorSettings(_env_file=None, key_dir=tmp_path / "keys"))
    monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: coordinator.url.replace("ws://", "http://").split("/peers")[0])
    rig = make_rig(monkeypatch, tmp_path, runner=contributor.run_contributor_loop)
    # make_rig patched the base to a fixed one; point it at the coordinator again.
    monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: coordinator.url.replace("ws://", "http://").split("/peers")[0])
    await rig.svc.apply_settings(settings(audience=audience, direct=direct))
    return rig


@run_async
async def test_the_first_attestation_already_carries_the_node_and_the_bound_port(monkeypatch, tmp_path):
    async with Coordinator(circle_links=True) as coord:
        rig = await run_service(monkeypatch, tmp_path, coord)
        await until(lambda: len(coord.attestations) >= 1)
        await asyncio.sleep(0.5)  # long enough for the capability watch to ask for a second one
        await rig.svc.stop()
    assert len(coord.attestations) == 1, "no extra reconnect at start"
    first = coord.attestations[0]
    assert set(first) == BASE_ATTEST_KEYS | set(FIXTURE["attestation"]["attest_added"])
    assert first["mesh_node_id"] == NODE and first["circle_port"] == 8347
    assert coord.accepts[0]["mesh_node_id"] == NODE and coord.accepts[0]["circle_links"] is True
    assert rig.listeners[0].host == MESH_IP


@run_async
async def test_an_older_coordinator_gets_the_fields_once_then_the_listener_goes_and_they_are_not_sent_again(
        monkeypatch, tmp_path):
    async with Coordinator(circle_links=None) as coord:
        rig = await run_service(monkeypatch, tmp_path, coord)
        await until(lambda: len(coord.attestations) >= 1)
        await asyncio.sleep(0.5)
        assert rig.up() is None, "the accept did not say circle_links: the listener is stopped"
        assert len(coord.attestations) == 1, "and that is no reason to reconnect"
        await coord.drop()
        await until(lambda: len(coord.attestations) >= 2)
        await asyncio.sleep(0.4)
        await coord.drop()
        await until(lambda: len(coord.attestations) >= 3)
        await rig.svc.stop()
    assert set(coord.attestations[0]) == BASE_ATTEST_KEYS | {"mesh_node_id", "circle_port"}, \
        "an older coordinator ignores the two keys"
    assert all(set(a) == BASE_ATTEST_KEYS for a in coord.attestations[1:]), "never again for this coordinator"
    assert len(rig.listeners) == 1


@run_async
async def test_a_coordinator_that_brokers_links_but_will_not_bind_ends_with_no_listener_and_no_flapping(
        monkeypatch, tmp_path):
    async with Coordinator(circle_links=True, echo_binding=False) as coord:
        rig = await run_service(monkeypatch, tmp_path, coord)
        await until(lambda: len(coord.attestations) >= 1)
        await asyncio.sleep(0.8)
        assert len(coord.attestations) == 1, "it holds nothing of the fields, so nothing is withdrawn"
        assert rig.up() is None
        await coord.drop()
        await until(lambda: len(coord.attestations) >= 2)
        await asyncio.sleep(0.5)
        await rig.svc.stop()
    assert coord.attestations[0]["mesh_node_id"] == NODE
    assert set(coord.attestations[1]) == BASE_ATTEST_KEYS and len(coord.attestations) == 2
    assert len(rig.listeners) == 1


@run_async
async def test_turning_direct_links_off_withdraws_the_listener_from_the_attestation(monkeypatch, tmp_path):
    async with Coordinator(circle_links=True) as coord:
        rig = await run_service(monkeypatch, tmp_path, coord)
        await until(lambda: len(coord.attestations) >= 1)
        await rig.svc.apply_settings(settings(direct=False))
        await until(lambda: len(coord.attestations) >= 2, timeout=5)
        await asyncio.sleep(0.4)
        await rig.svc.stop()
    assert "circle_port" in coord.attestations[0] and "circle_port" not in coord.attestations[1]
    assert len(coord.attestations) == 2 and rig.up() is None


@run_async
async def test_a_sidecar_that_cannot_be_read_at_start_means_a_bare_first_attestation_and_the_watcher_catches_up(
        monkeypatch, tmp_path):
    async with Coordinator(circle_links=True) as coord:
        rig = make_rig(monkeypatch, tmp_path, runner=contributor.run_contributor_loop)
        rig.sidecar["status"] = None
        from potluck_local.services import pool_routing

        monkeypatch.setattr(contributor, "CAPABILITY_CHECK_INTERVAL_S", 0.05)
        monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 0.05)
        monkeypatch.setattr(pool_routing, "coordinator_http_base",
                            lambda: coord.url.replace("ws://", "http://").split("/peers")[0])
        await rig.svc.apply_settings(settings())
        await until(lambda: len(coord.attestations) >= 1)
        assert set(coord.attestations[0]) == BASE_ATTEST_KEYS, "soft: attested without, not refused"
        rig.sidecar["status"] = status_with()
        await until(lambda: len(coord.attestations) >= 2, timeout=5)
        await rig.svc.stop()
    assert coord.attestations[1]["circle_port"] == 8347


@run_async
async def test_a_sidecar_that_does_not_answer_in_time_does_not_hold_up_the_connection(monkeypatch, tmp_path):
    monkeypatch.setattr(contributor, "CIRCLE_PREPARE_TIMEOUT_S", 0.2)
    async with Coordinator(circle_links=True) as coord:
        rig = make_rig(monkeypatch, tmp_path, runner=contributor.run_contributor_loop)
        from potluck_local.services import pool_routing

        monkeypatch.setattr(pool_routing, "coordinator_http_base",
                            lambda: coord.url.replace("ws://", "http://").split("/peers")[0])

        async def hangs():
            await asyncio.sleep(30)

        rig.svc._circle_status_reader = hangs
        started = asyncio.get_event_loop().time()
        await rig.svc.apply_settings(settings())
        await until(lambda: len(coord.attestations) >= 1, timeout=3)
        took = asyncio.get_event_loop().time() - started
        await rig.svc.stop()
    assert took < 1.5 and set(coord.attestations[0]) == BASE_ATTEST_KEYS


# ---- direct requests share the job cap and the heartbeat ---------------------------------


def _loop_settings(url: str) -> Settings:
    return Settings(coordinator_url=url, binary_hash="test-hash", member_id=uuid.UUID(int=1))


def _hooks(**over) -> contributor.LoopHooks:
    events: list = []

    async def job_handler(ws, job: JobAssignedMessage):
        await ws.send(json.dumps({"type": "job_complete", "job_id": str(job.job_id), "output_tokens": 0,
                                  "duration_ms": 1, "finish_reason": "stop"}))

    async def capacity():
        return Capacity()

    async def capabilities():
        return []

    hooks = contributor.LoopHooks(
        job_handler=job_handler, capacity_probe=capacity, capabilities_provider=capabilities,
        hardware_summary_provider=lambda: {}, headers_provider=lambda: None, max_concurrent_jobs=1,
        on_state=lambda event, **f: events.append((event, f)))
    for key, value in over.items():
        setattr(hooks, key, value)
    return hooks


async def wait_frame(coord, predicate, timeout=4.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for f in coord.frames:
            if predicate(f):
                return f
        await asyncio.sleep(0.02)
    raise AssertionError("frame not seen")


@run_async
async def test_a_direct_request_in_flight_makes_the_relay_decline_at_the_cap():
    direct = [1]
    async with Coordinator(heartbeat_interval_s=1) as coord:
        hooks = _hooks(direct_jobs=lambda: direct[0])
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _loop_settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord._connected.wait()
        job = await coord.assign()
        declined = await wait_frame(coord, lambda f: f.get("type") == "job_error" and f["job_id"] == str(job))
        assert declined["error_category"] == "declined" and "1 of 1" in declined["error_message"]
        direct[0] = 0
        job = await coord.assign()
        await wait_frame(coord, lambda f: f.get("type") == "job_complete" and f["job_id"] == str(job))
        stop.set()
        await asyncio.wait_for(task, 5)


@run_async
async def test_the_heartbeat_counts_direct_requests():
    async with Coordinator(heartbeat_interval_s=1) as coord:
        hooks = _hooks(direct_jobs=lambda: 2, max_concurrent_jobs=4)
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _loop_settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord._connected.wait()
        beat = await wait_frame(coord, lambda f: f.get("type") == "heartbeat")
        stop.set()
        await asyncio.wait_for(task, 5)
    assert beat["current_jobs"] == 2 and beat["max_concurrent_jobs"] == 4


@run_async
async def test_a_hook_that_fails_counts_as_none_and_changes_nothing():
    def broken():
        raise RuntimeError("x")

    async with Coordinator(heartbeat_interval_s=1) as coord:
        hooks = _hooks(direct_jobs=broken, circle_attest=broken)
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _loop_settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord._connected.wait()
        job = await coord.assign()
        await wait_frame(coord, lambda f: f.get("type") == "job_complete" and f["job_id"] == str(job))
        stop.set()
        await asyncio.wait_for(task, 5)
    assert set(coord.attestations[0]) == BASE_ATTEST_KEYS


# ---- the engine's own copies of the coordinator's shapes ---------------------------------------


def test_the_accept_reads_the_contracts_added_fields():
    base = {"type": "accept", "peer_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
            "server_time": "2026-10-09T00:00:00Z", "heartbeat_interval_s": 30}
    added = AcceptMessage.model_validate({**base, **FIXTURE["attestation"]["accept_added"]})
    assert added.circle_links is True and str(added.mesh_node_id) == NODE
    unbound = AcceptMessage.model_validate({**base, **FIXTURE["attestation"]["accept_added_unbound"]})
    assert unbound.circle_links is True and unbound.mesh_node_id is None
    off = AcceptMessage.model_validate({**base, **FIXTURE["attestation"]["accept_links_off"]})
    assert off.circle_links is None and off.mesh_node_id is None


@pytest.mark.parametrize("junk", [{"circle_links": "yes"}, {"circle_links": 1}, {"mesh_node_id": "nope"},
                                  {"mesh_node_id": 5}, {"circle_links": [], "mesh_node_id": {}}])
def test_junk_in_the_added_accept_fields_never_fails_an_accept(junk):
    base = {"type": "accept", "peer_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
            "server_time": "2026-10-09T00:00:00Z", "heartbeat_interval_s": 30}
    msg = AcceptMessage.model_validate({**base, **junk})
    assert msg.circle_links is None and msg.mesh_node_id is None


def test_the_attestation_fields_are_the_contracts():
    added = FIXTURE["attestation"]["attest_added"]
    assert set(added) == {"mesh_node_id", "circle_port"}
    fields = set(AttestationPayload.model_fields)
    assert set(added) <= fields
    payload = AttestationPayload(member_id=uuid.uuid4(), peer_public_key="k", binary_hash="b", client_version="v",
                                 platform="p", nonce_signature="s", **added)
    assert json.loads(payload.model_dump_json())["circle_port"] == added["circle_port"]
    with pytest.raises(Exception):
        AttestationPayload(member_id=uuid.uuid4(), peer_public_key="k", binary_hash="b", client_version="v",
                           platform="p", nonce_signature="s", circle_port=70000)


def test_a_reattestation_the_engine_asked_for_is_not_recorded_as_an_error(monkeypatch, tmp_path):
    rig = make_rig(monkeypatch, tmp_path)
    svc = rig.svc
    svc._on_state("connected", peer_id="p", session_id="s", heartbeat_interval_s=30)
    svc._on_state("reattest")
    st = svc.status()
    assert st["state"] == "connecting" and st["peer_id"] is None
    assert st["last_error"] is None and st["reconnects"] == 0
    svc._on_state("rejected", reason="unauthorized")
    svc._on_state("reattest")
    assert svc.status()["state"] == "rejected", "a rejection stays what it is"


@run_async
async def test_a_connection_being_prepared_while_the_service_stops_does_not_bring_the_listener_back(monkeypatch, tmp_path):
    class SlowToLeave(FakeLoopRunner):
        async def __call__(self, settings, priv, hooks, stop):
            self.starts += 1
            await stop.wait()
            await asyncio.sleep(0.3)  # the loop takes a moment to wind down
            hooks.on_state("stopped")

    rig = make_rig(monkeypatch, tmp_path, runner=SlowToLeave())
    await rig.svc.apply_settings(settings())
    await until(lambda: rig.up() is not None)
    stopping = asyncio.ensure_future(rig.svc.stop())
    await asyncio.sleep(0.1)
    assert rig.svc.is_running, "the loop is still winding down"
    await rig.svc.circle_prepare()  # what the loop does before it connects
    assert rig.up() is None and len(rig.listeners) == 1
    await stopping
    assert rig.up() is None and rig.svc.circle_attest() is None
