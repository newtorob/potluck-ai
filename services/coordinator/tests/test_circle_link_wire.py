"""Direct circle links over the real transports.

The kill switch and the HTTP routes through the app; hello.features and the ack
on /mesh/signal; the mesh node binding on /peers/connect; a create that is
answered by real websocket sidecars; and the lender-side events (sharing off,
disconnect) as they arrive from a real /peers/connect connection.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from coordinator.config import get_settings
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models import (
    CircleMembership,
    Household,
    MemberKey,
    MeshNode,
    Peer,
)
from coordinator.routers import circle_links as links_router
from coordinator.routers import circles, mesh, peers
from coordinator.services import attestation, circle_links, peer_registry
from tests.circle_link_helpers import FakeSidecar, build_world, issue_node_token, wg_key
from tests.test_circle_links import links_on, make_world  # noqa: F401  (fixtures)
from tests.test_contributor_m25 import _b64url, _cleanup, _pubkey, _seed_session


def make_app() -> FastAPI:
    from coordinator.db.database import close_db, init_db

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        try:
            yield
        finally:
            await close_db()

    app = FastAPI(lifespan=lifespan)
    for router in (peers.router, mesh.router, circles.router, links_router.router):
        app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)


def _env(monkeypatch, **flags: str) -> None:
    for name, value in flags.items():
        monkeypatch.setenv(f"POTLUCK_{name}", value)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The kill switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", [
    ("post", "/me/circles/{c}/links", {"requester_node_id": str(uuid.uuid4()), "model_slug": "m"}),
    ("post", "/me/circles/{c}/links", {"nonsense": True}),          # would be a 422 if switched on
    ("post", "/me/circles/{c}/links", None),
    ("post", "/me/circles/{c}/links/{l}/renew", {}),
    ("post", "/me/circles/{c}/links/{l}/renew", None),
    ("delete", "/me/circles/{c}/links/{l}", None),
])
def test_with_the_switch_off_every_link_endpoint_is_503_links_disabled(monkeypatch, path, method, body) -> None:
    monkeypatch.delenv("POTLUCK_CIRCLE_LINKS_ENABLED", raising=False)
    get_settings.cache_clear()
    assert get_settings().circle_links_enabled is False, "off is the default"
    with TestClient(make_app()) as client:
        url = path.format(c=uuid.uuid4(), l=uuid.uuid4())
        resp = getattr(client, method)(url, **({"json": body} if body is not None else {}))
        assert resp.status_code == 503
        assert resp.json() == {"detail": {
            "code": "links_disabled",
            "message": "Direct circle links are not available on this network.",
            "reason": None,
        }}
        # Not even a bad circle id gets as far as a 422.
        assert getattr(client, method)(url.replace(url.split("/")[3], "not-a-uuid")).status_code == 503


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage", "2", " 1 x"])
def test_only_a_clear_yes_switches_links_on(monkeypatch, value) -> None:
    monkeypatch.setenv("POTLUCK_CIRCLE_LINKS_ENABLED", value)
    get_settings.cache_clear()
    try:
        assert get_settings().circle_links_enabled is False
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_the_switch_reads_the_usual_spellings(monkeypatch, value) -> None:
    monkeypatch.setenv("POTLUCK_CIRCLE_LINKS_ENABLED", value)
    get_settings.cache_clear()
    try:
        assert get_settings().circle_links_enabled is True
    finally:
        get_settings.cache_clear()


def test_the_settings_defaults_and_clamps(monkeypatch) -> None:
    for name in ("LINKS_ENABLED", "LINK_DIRECT", "LINK_TTL_S", "LINK_BIND_WITHOUT_SESSION", "LINK_SWEEP_INTERVAL_S"):
        monkeypatch.delenv(f"POTLUCK_CIRCLE_{name}", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert (s.circle_links_enabled, s.circle_link_direct, s.circle_link_bind_without_session) == (False, False, False)
    assert (s.circle_link_ttl_s, s.circle_link_sweep_interval_s) == (600, 15)
    for given, want in (("20", 20), ("1", 5), ("0", 5), ("999999", 1800), ("junk", 600), ("-3", 5)):
        monkeypatch.setenv("POTLUCK_CIRCLE_LINK_TTL_S", given)
        get_settings.cache_clear()
        assert get_settings().circle_link_ttl_s == want, given
    get_settings.cache_clear()


def test_with_the_switch_on_the_routes_are_live_and_a_stranger_gets_404(monkeypatch) -> None:
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    try:
        with TestClient(make_app()) as client:
            body = {"requester_node_id": str(uuid.uuid4()), "model_slug": "m"}
            resp = client.post(f"/me/circles/{uuid.uuid4()}/links", json=body)
            assert (resp.status_code, resp.json()["detail"]["code"]) == (404, "circle_not_found")
            assert client.post(f"/me/circles/{uuid.uuid4()}/links", json={"x": 1}).status_code == 422
            resp = client.post(f"/me/circles/{uuid.uuid4()}/links/{uuid.uuid4()}/renew", json={})
            assert (resp.status_code, resp.json()["detail"]["code"]) == (404, "link_not_found")
            assert client.post(f"/me/circles/{uuid.uuid4()}/links/{uuid.uuid4()}/renew").status_code == 404
            assert client.delete(f"/me/circles/{uuid.uuid4()}/links/{uuid.uuid4()}").status_code == 204
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# hello.features and the ack on /mesh/signal
# ---------------------------------------------------------------------------


def _hello(ws, key: str, **extra) -> dict:
    ws.send_json({"type": "hello", "wg_public_key": key, **extra})
    return ws.receive_json()


def _wait(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.mark.asyncio
async def test_hello_features_are_recorded_per_connection_and_cleared_on_disconnect(make_world) -> None:
    w = await make_world(requester_connected=False)
    node = w.requester.node_id
    # Links are offered only on a connection opened with the node's own token, as
    # a sidecar that enrolled opens it; a connection without one is covered in
    # test_circle_link_node_token.py.
    auth = await issue_node_token(w.factory, node)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            assert _hello(ws, w.requester.wg_key, features=["circle_link_v1", "something_newer"])["type"] == "welcome"
            assert mesh.signal_node_features(node) == {"circle_link_v1", "something_newer"}
            assert mesh.signal_node_connected(node)
            # Say goodbye the way a real sidecar does and let the server finish
            # (leaving the `with` would cancel the handler instead).
            ws.close(1000)
            assert _wait(lambda: not mesh.signal_node_connected(node))
        assert mesh.signal_node_features(node) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("features", [
    "circle_link_v1", 5, {"a": 1}, [None, {"a": 1}, 7, ["x"]], ["Has Space", "ÜBER", "x" * 100],
    [f"f{i}" for i in range(500)], True, 1.5,
])
async def test_a_bad_features_value_never_closes_the_signal_channel(make_world, features) -> None:
    w = await make_world(requester_connected=False)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal") as ws:
            welcome = _hello(ws, w.requester.wg_key, features=features)
            assert welcome["type"] == "welcome", welcome
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong", "the channel is still open"
            assert len(mesh.signal_node_features(w.requester.node_id)) <= 16


@pytest.mark.asyncio
async def test_a_sidecar_with_no_features_is_not_selectable_for_a_link(make_world) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal") as req_ws, client.websocket_connect("/mesh/signal") as len_ws:
            assert _hello(req_ws, w.requester.wg_key)["type"] == "welcome"           # old sidecar
            assert _hello(len_ws, w.lender.wg_key)["type"] == "welcome"
            assert mesh.signal_node_features(w.requester.node_id) == frozenset()
            from tests.test_circle_links import code_of, create, refused
            exc = await refused(create(w))
            assert code_of(exc) == "requester_sidecar_unsupported"
            _hello_features = req_ws  # noqa: F841


@pytest.mark.asyncio
async def test_unknown_messages_and_bad_acks_do_not_end_the_signal_loop(make_world) -> None:
    w = await make_world(requester_connected=False)
    # Token-bound, so these acks reach the ack handler (a legacy connection's acks
    # are dropped before it).
    auth = await issue_node_token(w.factory, w.requester.node_id)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            assert _hello(ws, w.requester.wg_key, features=["circle_link_v1"])["type"] == "welcome"
            for junk in (
                {"type": "circle_peer_future_thing", "x": 1},
                {"type": "circle_peer_ack"},
                {"type": "circle_peer_ack", "link_id": "nope", "op": "add", "state": "ready"},
                {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "add", "state": "ready"},
                {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "add", "state": "rejected",
                 "reason": "x" * 10_000},
                [],
                "text",
                7,
            ):
                ws.send_json(junk)
                ws.send_json({"type": "ping"})
                assert ws.receive_json()["type"] == "pong", junk


# ---------------------------------------------------------------------------
# A create answered by real websocket sidecars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_over_the_real_transport_end_to_end(make_world, links_on, monkeypatch) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    result: dict = {}

    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws, \
                client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
            assert _hello(req_ws, w.requester.wg_key, features=["circle_link_v1"])["type"] == "welcome"
            assert _hello(len_ws, w.lender.wg_key, features=["circle_link_v1"])["type"] == "welcome"

            def post() -> None:
                result["resp"] = client.post(
                    f"/me/circles/{w.circle_id}/links",
                    json={"requester_node_id": str(w.requester.node_id), "model_slug": w.model},
                )

            thread = threading.Thread(target=post)
            thread.start()
            to_requester, to_lender = req_ws.receive_json(), len_ws.receive_json()
            assert (to_requester["type"], to_requester["role"]) == ("circle_peer_add", "requester")
            assert (to_lender["type"], to_lender["role"]) == ("circle_peer_add", "lender")
            for ws, msg in ((req_ws, to_requester), (len_ws, to_lender)):
                ws.send_json({"type": "circle_peer_ack", "link_id": msg["link_id"], "op": "add",
                              "state": "ready", "reason": None})
            thread.join(timeout=5)
            resp = result["resp"]
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["ready"] is True and body["lender"] == {"mesh_ip": w.lender.mesh_ip, "port": 8347}
            assert to_lender["link_id"] == body["link_id"]

            # The lender's engine would now check this hash against the token.
            import hashlib
            assert to_lender["token_sha256"] == hashlib.sha256(body["token"].encode()).hexdigest()

            # Renew over the same transport (half a lifetime on, as an engine would).
            from tests.test_circle_links import settle
            await settle(w, body["link_id"])
            result.clear()
            thread = threading.Thread(target=lambda: result.update(resp=client.post(
                f"/me/circles/{w.circle_id}/links/{body['link_id']}/renew", json={})))
            thread.start()
            ext_r, ext_l = req_ws.receive_json(), len_ws.receive_json()
            assert ext_r["type"] == ext_l["type"] == "circle_peer_extend"
            for ws, msg in ((req_ws, ext_r), (len_ws, ext_l)):
                ws.send_json({"type": "circle_peer_ack", "link_id": msg["link_id"], "op": "extend",
                              "state": "ready", "reason": None})
            thread.join(timeout=5)
            assert result["resp"].status_code == 200
            assert result["resp"].json()["delivered"] == {"requester": True, "lender": True}

            # And end it: both sidecars are told.
            assert client.delete(f"/me/circles/{w.circle_id}/links/{body['link_id']}").status_code == 204
            assert req_ws.receive_json() == {"type": "circle_peer_remove", "link_id": body["link_id"], "reason": "ended"}
            assert len_ws.receive_json() == {"type": "circle_peer_remove", "link_id": body["link_id"], "reason": "ended"}


@pytest.mark.asyncio
async def test_a_silent_websocket_sidecar_is_a_504_over_http(make_world, links_on, monkeypatch) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 1.0)
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws, \
                client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
            _hello(req_ws, w.requester.wg_key, features=["circle_link_v1"])
            _hello(len_ws, w.lender.wg_key, features=["circle_link_v1"])
            result: dict = {}
            thread = threading.Thread(target=lambda: result.update(resp=client.post(
                f"/me/circles/{w.circle_id}/links",
                json={"requester_node_id": str(w.requester.node_id), "model_slug": w.model})))
            thread.start()
            add_r = req_ws.receive_json()
            req_ws.send_json({"type": "circle_peer_ack", "link_id": add_r["link_id"], "op": "add",
                              "state": "ready", "reason": None})
            len_ws.receive_json()                      # the lender never answers
            thread.join(timeout=5)
            resp = result["resp"]
            assert (resp.status_code, resp.json()["detail"]["code"]) == (504, "sidecar_timeout")
            assert req_ws.receive_json()["type"] == "circle_peer_remove"
            assert len_ws.receive_json()["type"] == "circle_peer_remove"


@pytest.mark.asyncio
async def test_a_sidecar_that_reconnects_is_sent_its_live_links(make_world, links_on, monkeypatch) -> None:
    w = await make_world()
    out = await __import__("tests.test_circle_links", fromlist=["create"]).create(w)
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    auth = await issue_node_token(w.factory, w.lender.node_id)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            assert _hello(ws, w.lender.wg_key, features=["circle_link_v1"])["type"] == "welcome"
            add = ws.receive_json()
            assert (add["type"], add["role"], add["link_id"]) == ("circle_peer_add", "lender", str(out.link_id))
        # A sidecar that does not advertise links is not sent them (token-bound,
        # so it is the missing feature that keeps them back, not the missing token).
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            assert _hello(ws, w.lender.wg_key)["type"] == "welcome"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


# ---------------------------------------------------------------------------
# Binding an engine to its mesh node at attestation
# ---------------------------------------------------------------------------


def _attest(ws, priv, member_id, **extra) -> dict:
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
    ws.send_json({
        "type": "attest", "member_id": str(member_id), "peer_public_key": _pubkey(priv),
        "binary_hash": "hash-" + uuid.uuid4().hex, "client_version": "0.0.2-test", "platform": "linux-x86_64",
        "hardware_summary": {}, "nonce_signature": _b64url(priv.sign(nonce)), **extra,
    })
    return ws.receive_json()


async def _household_with_node(factory, member_id: uuid.UUID) -> uuid.UUID:
    from tests.circle_link_helpers import fresh_address

    async with factory() as db:
        async with db.begin():
            cidr, ip = await fresh_address(db)
            hh = Household(owner_member_id=member_id, name="bind-test", mesh_cidr=cidr)
            db.add(hh)
            await db.flush()
            node = MeshNode(household_id=hh.id, node_name="bind-box", mesh_ip=ip, wg_public_key=wg_key())
            db.add(node)
            await db.flush()
    return node.id


async def _drop_households(factory, member_ids) -> None:
    async with factory() as db:
        await db.execute(delete(Household).where(Household.owner_member_id.in_(list(member_ids))))
        await db.commit()


@pytest.fixture()
def bound_env(monkeypatch):
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1", CIRCLE_LINK_BIND_WITHOUT_SESSION="1")
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_an_engine_is_bound_to_a_mesh_node_its_owner_owns(session_factory, bound_env) -> None:
    member = uuid.uuid4()
    node_id = await _household_with_node(session_factory.factory, member)
    priv = Ed25519PrivateKey.generate()
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, mesh_node_id=str(node_id), circle_port=8347)
                assert accept["type"] == "accept", accept
                assert accept["mesh_node_id"] == str(node_id) and accept["circle_links"] is True
                conn = client.portal.call(lambda: peer_registry.get_peer(uuid.UUID(accept["peer_id"])))
                assert (conn.mesh_node_id, conn.circle_port) == (node_id, 8347)
    finally:
        await _drop_households(session_factory.factory, [member])
        await _cleanup(pubkeys=[_pubkey(priv)]) if False else None
        async with session_factory.factory() as db:
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.execute(text("DELETE FROM peers WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["someone_elses", "no_such_node", "not_a_uuid", "absent", "bad_port",
                                  "port_as_text", "zero_port"])
async def test_a_binding_that_cannot_be_honoured_is_soft_refused_and_the_relay_still_works(
    session_factory, bound_env, case
) -> None:
    member, other = uuid.uuid4(), uuid.uuid4()
    others_node = await _household_with_node(session_factory.factory, other)
    priv = Ed25519PrivateKey.generate()
    extra = {
        "someone_elses": {"mesh_node_id": str(others_node), "circle_port": 8340},
        "no_such_node": {"mesh_node_id": str(uuid.uuid4()), "circle_port": 8340},
        "not_a_uuid": {"mesh_node_id": "not-a-uuid", "circle_port": 8340},
        "absent": {},                                                    # an engine from before links
        "bad_port": {"mesh_node_id": str(others_node), "circle_port": 70000},
        "port_as_text": {"mesh_node_id": str(others_node), "circle_port": "8340"},
        "zero_port": {"circle_port": 0},
    }[case]
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, **extra)
                assert accept["type"] == "accept", f"never rejected over a binding: {accept}"
                assert "mesh_node_id" not in accept, "nothing echoed for a node that was not bound"
                assert accept["circle_links"] is True
                conn = client.portal.call(lambda: peer_registry.get_peer(uuid.UUID(accept["peer_id"])))
                assert conn is not None, "still on the relay"
                assert conn.mesh_node_id is None and conn.circle_port is None
    finally:
        await _drop_households(session_factory.factory, [other])
        async with session_factory.factory() as db:
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.execute(text("DELETE FROM peers WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.commit()


@pytest.mark.asyncio
async def test_a_shared_token_engine_is_not_bound_unless_the_development_flag_is_set(
    session_factory, monkeypatch
) -> None:
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")  # bind_without_session stays off
    member = uuid.uuid4()
    node_id = await _household_with_node(session_factory.factory, member)
    priv = Ed25519PrivateKey.generate()
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, mesh_node_id=str(node_id), circle_port=8340)
                assert accept["type"] == "accept" and "mesh_node_id" not in accept
                assert accept["circle_links"] is True
    finally:
        get_settings.cache_clear()
        await _drop_households(session_factory.factory, [member])
        async with session_factory.factory() as db:
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.execute(text("DELETE FROM peers WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.commit()


@pytest.mark.asyncio
async def test_a_member_session_binds_without_the_development_flag(session_factory, monkeypatch) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")  # bind_without_session off
    member = uuid.uuid4()
    node_id = await _household_with_node(session_factory.factory, member)
    priv = Ed25519PrivateKey.generate()
    token = "sess-" + uuid.uuid4().hex
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            client.portal.call(lambda: _seed_session(member, token))
            with client.websocket_connect("/peers/connect", headers={"Authorization": f"Bearer {token}"}) as ws:
                # The payload's member_id is a stale stub; the session is the identity.
                accept = _attest(ws, priv, uuid.uuid4(), mesh_node_id=str(node_id), circle_port=8340)
                assert accept["type"] == "accept", accept
                assert accept["mesh_node_id"] == str(node_id)
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)], members=[member]))
    finally:
        await _drop_households(session_factory.factory, [member])


@pytest.mark.asyncio
async def test_a_member_session_cannot_bind_a_node_in_someone_elses_household(session_factory, monkeypatch) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    member, other = uuid.uuid4(), uuid.uuid4()
    others_node = await _household_with_node(session_factory.factory, other)
    priv = Ed25519PrivateKey.generate()
    token = "sess-" + uuid.uuid4().hex
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            client.portal.call(lambda: _seed_session(member, token))
            with client.websocket_connect("/peers/connect", headers={"Authorization": f"Bearer {token}"}) as ws:
                accept = _attest(ws, priv, member, mesh_node_id=str(others_node), circle_port=8340)
                assert accept["type"] == "accept" and "mesh_node_id" not in accept
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)], members=[member]))
    finally:
        await _drop_households(session_factory.factory, [other])


@pytest.mark.asyncio
async def test_with_links_off_the_accept_is_exactly_what_it_was_before(session_factory, monkeypatch) -> None:
    monkeypatch.delenv("POTLUCK_CIRCLE_LINKS_ENABLED", raising=False)
    get_settings.cache_clear()
    member = uuid.uuid4()
    node_id = await _household_with_node(session_factory.factory, member)
    priv = Ed25519PrivateKey.generate()
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, mesh_node_id=str(node_id), circle_port=8340, audiences=["circle"])
                assert accept["type"] == "accept"
                assert set(accept) == {"type", "peer_id", "session_id", "server_time",
                                       "heartbeat_interval_s", "audiences"}
                conn = client.portal.call(lambda: peer_registry.get_peer(uuid.UUID(accept["peer_id"])))
                assert (conn.mesh_node_id, conn.circle_port) == (None, None), "no binding while off"
    finally:
        await _drop_households(session_factory.factory, [member])
        async with session_factory.factory() as db:
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.execute(text("DELETE FROM peers WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raises", "database_error"])
async def test_a_binding_step_that_fails_never_rejects_the_attestation(
    session_factory, bound_env, how
) -> None:
    """Binding is a soft step: if it blows up, the machine is attested, accepted
    and on the relay exactly as before, just not offered for links."""
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import AsyncSession

    member = uuid.uuid4()
    node_id = await _household_with_node(session_factory.factory, member)
    priv = Ed25519PrivateKey.generate()
    if how == "raises":
        async def boom(*a, **k):
            raise RuntimeError("binding bug")
        bound_env.setattr("coordinator.routers.peers.resolve_mesh_binding", boom)
    else:
        real = AsyncSession.scalar

        async def flaky(self, stmt, *a, **k):
            if "mesh_nodes" in str(stmt):
                raise ProgrammingError("SELECT", {}, Exception("relation does not exist"))
            return await real(self, stmt, *a, **k)

        bound_env.setattr(AsyncSession, "scalar", flaky)
    try:
        with TestClient(make_app(), raise_server_exceptions=False) as client:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, mesh_node_id=str(node_id), circle_port=8340)
                assert accept["type"] == "accept", accept
                assert "mesh_node_id" not in accept and accept["circle_links"] is True
                conn = client.portal.call(lambda: peer_registry.get_peer(uuid.UUID(accept["peer_id"])))
                assert conn is not None and conn.mesh_node_id is None
                # And the connection carries on: a heartbeat is still applied.
                ws.send_json({"type": "heartbeat", "peer_id": accept["peer_id"], "session_id": accept["session_id"],
                              "timestamp": "2026-10-09T12:00:00Z", "current_jobs": 0})
    finally:
        await _drop_households(session_factory.factory, [member])
        async with session_factory.factory() as db:
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.execute(text("DELETE FROM peers WHERE public_key = :k"), {"k": _pubkey(priv)})
            await db.commit()


@pytest.mark.asyncio
async def test_resolve_mesh_binding_never_raises(session_factory, bound_env) -> None:
    from coordinator.schemas.peer import AttestationPayload

    payload = AttestationPayload(
        member_id=uuid.uuid4(), peer_public_key="k", binary_hash="h", client_version="v",
        platform="p", nonce_signature="s", mesh_node_id=uuid.uuid4(), circle_port=8340)

    class Broken:
        rolled_back = False

        async def scalar(self, *a, **k):
            raise RuntimeError("database went away")

        async def rollback(self):
            Broken.rolled_back = True
            raise RuntimeError("and so did the rollback")

    got = await attestation.resolve_mesh_binding(
        Broken(), payload, payload.member_id, session_member_id=None)
    assert got is None and Broken.rolled_back


# ---------------------------------------------------------------------------
# Lender events as a real engine connection delivers them
# ---------------------------------------------------------------------------


async def _lender_via_attestation(w, client, priv, member, *, audiences):
    """Attach a real /peers/connect engine as the world's lender: its household
    and node, a fake sidecar on the mesh, membership in the circle."""
    node_id = await _household_with_node(w.factory, member)
    async with w.factory() as db:
        node = await db.get(MeshNode, node_id)
    sidecar = FakeSidecar(node_id)
    mesh._signal_connections[node_id] = sidecar
    mesh._signal_features[node_id] = frozenset({circle_links.LINK_FEATURE})
    async with w.factory() as db:
        db.add(CircleMembership(circle_id=w.circle_id, member_id=member, role="member"))
        await db.commit()
    return node, sidecar


@pytest.mark.asyncio
async def test_a_real_lender_engine_reconnecting_without_circles_ends_its_links(
    make_world, session_factory, bound_env
) -> None:
    from tests.test_circle_links import create

    w = await make_world(lenders=0)
    member = uuid.uuid4()
    priv = Ed25519PrivateKey.generate()
    caps = [{"model_slug": w.model, "quantization": "q4"}]
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        node, lender_car = await _lender_via_attestation(w, client, priv, member, audiences=None)
        try:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, capabilities=caps, audiences=["circle"],
                                 mesh_node_id=str(node.id), circle_port=8347)
                assert accept["mesh_node_id"] == str(node.id)
                out = await create(w)
                assert out.lender.mesh_ip == str(node.mesh_ip)
                assert (await w.row(out.link_id)).ended_at is None

                # Same key, new connection, now for the open pool only.
                with client.websocket_connect("/peers/connect") as ws2:
                    accept2 = _attest(ws2, priv, member, capabilities=caps, audiences=["pool"],
                                      mesh_node_id=str(node.id), circle_port=8347)
                    assert accept2["audiences"] == ["pool"]
                    assert _wait(lambda: any(m["type"] == "circle_peer_remove" for m in lender_car.sent))
                row = await w.row(out.link_id)
                assert row.end_reason == "sharing_off"
                removes = [m for m in lender_car.sent if m["type"] == "circle_peer_remove"]
                assert removes[0]["reason"] == "revoked"
        finally:
            mesh._signal_connections.pop(node.id, None)
            mesh._signal_features.pop(node.id, None)
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))
            await _drop_households(w.factory, [member])


@pytest.mark.asyncio
async def test_a_real_lender_engine_disconnecting_ends_its_links_after_the_grace(
    make_world, session_factory, bound_env, monkeypatch
) -> None:
    from tests.test_circle_links import create

    monkeypatch.setattr(circle_links, "LENDER_DISCONNECT_GRACE_S", 1.0)
    w = await make_world(lenders=0)
    member = uuid.uuid4()
    priv = Ed25519PrivateKey.generate()
    caps = [{"model_slug": w.model, "quantization": "q4"}]
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        node, lender_car = await _lender_via_attestation(w, client, priv, member, audiences=None)
        try:
            with client.websocket_connect("/peers/connect") as ws:
                accept = _attest(ws, priv, member, capabilities=caps, audiences=["circle"],
                                 mesh_node_id=str(node.id), circle_port=8347)
                peer_id = uuid.UUID(accept["peer_id"])
                out = await create(w)
                ws.close(1000)          # the engine goes away
                assert _wait(lambda: client.portal.call(lambda: peer_registry.get_peer(peer_id)) is None)
            assert (await w.row(out.link_id)).ended_at is None, "inside the grace"
            assert _wait(lambda: any(m["type"] == "circle_peer_remove" for m in lender_car.sent), timeout=6)
            assert (await w.row(out.link_id)).end_reason == "lender_gone"

            # A lender that reconnects inside the grace keeps its link.
            with client.websocket_connect("/peers/connect") as ws:
                _attest(ws, priv, member, capabilities=caps, audiences=["circle"],
                        mesh_node_id=str(node.id), circle_port=8347)
                out2 = await create(w)
                ws.close(1000)
                assert _wait(lambda: client.portal.call(lambda: peer_registry.get_peer(peer_id)) is None)
            with client.websocket_connect("/peers/connect") as ws:
                _attest(ws, priv, member, capabilities=caps, audiences=["circle"],
                        mesh_node_id=str(node.id), circle_port=8347)
                time.sleep(1.8)         # well past the grace
                assert (await w.row(out2.link_id)).ended_at is None
        finally:
            mesh._signal_connections.pop(node.id, None)
            mesh._signal_features.pop(node.id, None)
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))
            await _drop_households(w.factory, [member])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env,links,bind,status", [
    ("production", "1", "1", "fail"),     # trusting a shared-token engine's node claim on a network
    ("staging", "1", "1", "fail"),
    ("production", "1", "0", "ok"),
    ("production", "0", "1", "ok"),       # bind flag is inert while links are off
    ("development", "1", "1", "ok"),      # the harness
    ("production", "0", "0", "ok"),
])
def test_preflight_refuses_the_development_binding_flag_on_a_network(monkeypatch, env, links, bind, status) -> None:
    from coordinator.config import Settings
    from coordinator.services.preflight import run_preflight

    monkeypatch.setenv("POTLUCK_CIRCLE_LINKS_ENABLED", links)
    monkeypatch.setenv("POTLUCK_CIRCLE_LINK_BIND_WITHOUT_SESSION", bind)
    check = {c.name: c for c in run_preflight(Settings(env=env))}["circle_link_bind_without_session"]
    assert check.status == status
    assert check.blocking is True
