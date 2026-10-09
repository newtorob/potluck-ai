"""Direct circle links are offered only on a signal connection that the node's own
node_token opened.

A WireGuard public key is not a secret: every household peer and every circle
peer learns it. On the legacy path of /mesh/signal a hello that merely names a
node by its key is accepted (no coordinator token is configured, or the shared
one was presented), so without this rule anyone who knew a node's key could open
its signal channel, advertise circle_link_v1 and receive the link messages meant
for that machine: the other household's key, mesh address and endpoint, and for a
lender the token hash.

What these tests pin:

* a token-bound hello with the feature is offered links; a legacy hello with the
  feature is not, and no circle message is ever written to it;
* a legacy hello still joins, gets its welcome and takes part in the household
  mesh exactly as before;
* a legacy hello that displaces a token-bound connection leaves the link side
  failed closed (the displacement case);
* every place the coordinator sends a link message, or picks a machine for a
  link, stays clear of a connection that lost the feature;
* `token_bound` is in the mesh_signal_connected log line;
* a node token for a different node is still refused.

The sidecars here are real websockets for the transport tests and FakeSidecars
(tests/circle_link_helpers.py) for the table of send paths. A token is given to a
node row the way /mesh/enroll does it (issue_node_token), and presented the way a
sidecar presents it: Authorization: Bearer on the upgrade request.
"""
from __future__ import annotations

import ast
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect
from structlog.testing import capture_logs

from coordinator.models import MeshNode
from coordinator.routers import circles as circles_router
from coordinator.routers import mesh
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.services import circle_links
from tests.circle_link_helpers import FakeSidecar, issue_node_token, wg_key
from tests.test_circle_link_wire import _env, _hello, _wait, make_app
from tests.test_circle_links import (  # noqa: F401  (fixtures)
    code_of,
    create,
    end,
    links_on,
    make_world,
    refused,
    renew,
    settle,
)
from tests.test_node_token_auth import (  # noqa: F401  (client is a fixture)
    _enroll,
    _seed_household_and_pairing_token,
    _wg_key_of,
    client as enroll_client,
)

LINK = circle_links.LINK_FEATURE


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)


def _pong_next(ws) -> None:
    """The next thing the server sends on `ws` is the answer to this ping. Frames
    on a socket arrive in the order they were written, so this shows that nothing
    else (no circle message, no roster) was written to it before the ping."""
    ws.send_json({"type": "ping"})
    assert ws.receive_json()["type"] == "pong"


def _post_in_thread(client, method: str, url: str, **kwargs):
    """Start an HTTP call that waits on the sidecars' acks, so the test thread can
    play the sidecars. Returns (thread, result dict)."""
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(resp=getattr(client, method)(url, **kwargs))
    )
    thread.start()
    return thread, result


def _ack(ws, message: dict, op: str) -> None:
    ws.send_json({"type": "circle_peer_ack", "link_id": message["link_id"], "op": op,
                  "state": "ready", "reason": None})


# ---------------------------------------------------------------------------
# Token-bound versus legacy: what the coordinator keeps and offers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_token_bound_hello_with_the_feature_is_offered_links(make_world) -> None:
    w = await make_world(requester_connected=False)
    node = w.requester.node_id
    auth = await issue_node_token(w.factory, node)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            assert _hello(ws, w.requester.wg_key, features=[LINK, "something_newer"])["type"] == "welcome"
            assert mesh.signal_node_features(node) == {LINK, "something_newer"}
            assert mesh.signal_node_connected(node)


def test_a_node_that_enrolled_is_token_bound_and_offered_links(enroll_client) -> None:
    """The whole chain a real sidecar takes: pair, receive the node token once,
    then present it on every dial."""
    client = enroll_client
    household_id, pairing_token = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, pairing_token).json()
    node = uuid.UUID(enrolled["node_id"])
    key = _wg_key_of(client, enrolled)
    with client.websocket_connect(
        "/mesh/signal", headers={"Authorization": f"Bearer {enrolled['node_token']}"}
    ) as ws:
        assert _hello(ws, key, features=[LINK])["type"] == "welcome"
        assert LINK in mesh.signal_node_features(node)
    # The same node by its key alone: joined, but links are withheld.
    with client.websocket_connect("/mesh/signal") as ws:
        assert _hello(ws, key, features=[LINK])["type"] == "welcome"
        assert LINK not in mesh.signal_node_features(node)


@pytest.mark.asyncio
async def test_a_legacy_hello_with_the_feature_is_not_offered_links(make_world) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    with TestClient(make_app()) as client:
        # Both machines on the legacy path.
        with client.websocket_connect("/mesh/signal") as req_ws, client.websocket_connect("/mesh/signal") as len_ws:
            assert _hello(req_ws, w.requester.wg_key, features=[LINK, "something_newer"])["type"] == "welcome"
            assert _hello(len_ws, w.lender.wg_key, features=[LINK])["type"] == "welcome"
            # Connected, but the link feature is gone and the rest is kept.
            assert mesh.signal_node_connected(w.requester.node_id)
            assert mesh.signal_node_features(w.requester.node_id) == {"something_newer"}
            assert mesh.signal_node_features(w.lender.node_id) == frozenset()

            # A requester on the legacy path is answered as one with no feature at all.
            exc = await refused(create(w))
            assert (exc.status_code, code_of(exc)) == (409, "requester_sidecar_unsupported")
            assert await w.rows() == []
            _pong_next(req_ws)
            _pong_next(len_ws)
            req_ws.close(1000)
            assert _wait(lambda: not mesh.signal_node_connected(w.requester.node_id))

        # A token-bound requester, and a lender that is still on the legacy path: the
        # lender is never picked, and nothing is written to its socket.
        auth = await issue_node_token(w.factory, w.requester.node_id)
        with client.websocket_connect("/mesh/signal", headers=auth) as req_ws, \
                client.websocket_connect("/mesh/signal") as len_ws:
            assert _hello(req_ws, w.requester.wg_key, features=[LINK])["type"] == "welcome"
            assert _hello(len_ws, w.lender.wg_key, features=[LINK])["type"] == "welcome"
            assert LINK in mesh.signal_node_features(w.requester.node_id)
            exc = await refused(create(w))
            assert (exc.status_code, code_of(exc)) == (409, "no_lender")
            assert await w.rows() == []
            _pong_next(len_ws)
            _pong_next(req_ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["unknown_bearer", "coordinator_token_only", "unknown_bearer_and_coordinator_token"])
async def test_only_the_nodes_own_token_makes_a_connection_token_bound(make_world, monkeypatch, how) -> None:
    """Other ways to get a hello accepted do not count: an unknown bearer falls
    through to the legacy gate, and so does the shared coordinator token."""
    w = await make_world(requester_connected=False)
    node = w.requester.node_id
    if how == "unknown_bearer":
        headers = {"Authorization": "Bearer " + "x" * 43}
    elif how == "coordinator_token_only":
        monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "shared-secret")
        headers = {"X-Potluck-Coordinator-Token": "shared-secret"}
    else:
        monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "shared-secret")
        headers = {"Authorization": "Bearer " + "y" * 43, "X-Potluck-Coordinator-Token": "shared-secret"}
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=headers) as ws:
                assert _hello(ws, w.requester.wg_key, features=[LINK])["type"] == "welcome"
                _pong_next(ws)
                assert LINK not in mesh.signal_node_features(node)
    (connected,) = [e for e in logs if e["event"] == "mesh_signal_connected"]
    assert connected["token_bound"] is False


@pytest.mark.asyncio
async def test_the_node_token_wins_when_the_coordinator_token_is_sent_too(make_world, monkeypatch) -> None:
    """The Go sidecar sends both headers whenever it has both."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "shared-secret")
    w = await make_world(requester_connected=False)
    auth = await issue_node_token(w.factory, w.requester.node_id)
    headers = {**auth, "X-Potluck-Coordinator-Token": "shared-secret"}
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=headers) as ws:
            assert _hello(ws, w.requester.wg_key, features=[LINK])["type"] == "welcome"
            assert LINK in mesh.signal_node_features(w.requester.node_id)


@pytest.mark.asyncio
async def test_a_node_token_for_a_different_node_is_still_rejected(make_world) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    requesters_auth = await issue_node_token(w.factory, w.requester.node_id)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=requesters_auth) as ws:
            ws.send_json({"type": "hello", "wg_public_key": w.lender.wg_key, "features": [LINK]})
            msg = ws.receive_json()
            assert (msg["type"], msg["code"]) == ("error", "unauthorized")
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    assert not mesh.signal_node_connected(w.lender.node_id)
    assert mesh.signal_node_features(w.lender.node_id) == frozenset()


# ---------------------------------------------------------------------------
# A legacy connection still belongs to the household mesh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_legacy_hello_still_gets_its_welcome_and_roster_updates(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    first = w.requester
    second_key = wg_key()
    prefix = first.mesh_ip.rsplit(".", 1)[0]
    async with w.factory() as db:
        async with db.begin():
            second = MeshNode(household_id=first.household_id, node_name="second-box",
                              mesh_ip=f"{prefix}.3", wg_public_key=second_key, platform="linux")
            db.add(second)
        second_id = second.id

    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal") as ws1, client.websocket_connect("/mesh/signal") as ws2:
            welcome = _hello(ws1, first.wg_key, features=[LINK])
            assert welcome["type"] == "welcome"
            assert welcome["node_id"] == str(first.node_id) and welcome["mesh_ip"] == first.mesh_ip
            assert [p["node_id"] for p in welcome["roster"]] == [str(second_id)]

            assert _hello(ws2, second_key)["type"] == "welcome"
            update = ws1.receive_json()                   # the household hears the other node join
            assert update["type"] == "roster_update"
            assert [p["node_id"] for p in update["added"]] == [str(second_id)]

            ws2.send_json({"type": "endpoint_update", "external_endpoint": "203.0.113.5:51820"})
            update = ws1.receive_json()
            assert update["type"] == "roster_update"
            assert [(p["node_id"], p["last_endpoint"]) for p in update["updated"]] == [
                (str(second_id), "203.0.113.5:51820")]

            # ... and the legacy node's own endpoint and punch requests are honoured.
            ws1.send_json({"type": "endpoint_update", "external_endpoint": "203.0.113.9:51820"})
            assert ws2.receive_json()["updated"][0]["last_endpoint"] == "203.0.113.9:51820"
            ws1.send_json({"type": "request_punch", "target_node_id": str(second_id)})
            assert ws1.receive_json()["role"] == "initiator"
            assert ws2.receive_json()["role"] == "responder"
            _pong_next(ws1)
            assert LINK not in mesh.signal_node_features(first.node_id)


# ---------------------------------------------------------------------------
# token_bound in the log, and the one line for a withheld feature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_bound_is_logged_and_a_withheld_feature_is_logged_once(make_world) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    auth = await issue_node_token(w.factory, w.requester.node_id)
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=auth) as bound, \
                    client.websocket_connect("/mesh/signal") as legacy:
                _hello(bound, w.requester.wg_key, features=[LINK])
                _hello(legacy, w.lender.wg_key, features=[LINK])
                _pong_next(bound)
                _pong_next(legacy)
    connected = {e["node_id"]: e for e in logs if e["event"] == "mesh_signal_connected"}
    assert connected[str(w.requester.node_id)]["token_bound"] is True
    assert connected[str(w.lender.node_id)]["token_bound"] is False
    assert {"node_id", "household_id", "roster_size", "token_bound"} <= set(connected[str(w.lender.node_id)])

    withheld = [e for e in logs if e["event"] == "mesh_signal_links_need_node_token"]
    assert len(withheld) == 1, "one line, for the connection that lost the feature"
    assert withheld[0]["node_id"] == str(w.lender.node_id)
    assert set(withheld[0]) <= {"event", "log_level", "node_id"}, "nothing but the node id"
    assert w.lender.wg_key not in repr(logs) and w.requester.wg_key not in repr(logs)


@pytest.mark.asyncio
async def test_a_legacy_hello_that_never_asked_for_links_has_nothing_withheld(make_world) -> None:
    w = await make_world(requester_connected=False)
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal") as ws:
                _hello(ws, w.requester.wg_key, features=["something_newer"])
                _pong_next(ws)
                assert mesh.signal_node_features(w.requester.node_id) == {"something_newer"}
    assert [e for e in logs if e["event"] == "mesh_signal_links_need_node_token"] == []


@pytest.mark.asyncio
async def test_acks_on_a_legacy_connection_wake_nothing(make_world, monkeypatch) -> None:
    """No add or extend is ever sent to a legacy connection, so an ack on one can
    only be made up; it must not satisfy a wait for the real sidecar."""
    w = await make_world(requester_connected=False)
    seen: list = []

    async def spy(node_id, raw):
        seen.append(node_id)

    monkeypatch.setattr(circle_links, "handle_ack", spy)
    auth = await issue_node_token(w.factory, w.requester.node_id)
    ack = {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "add", "state": "ready"}
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal") as ws:
            _hello(ws, w.requester.wg_key, features=[LINK])
            ws.send_json(ack)
            _pong_next(ws)
        assert seen == []
        with client.websocket_connect("/mesh/signal", headers=auth) as ws:
            _hello(ws, w.requester.wg_key, features=[LINK])
            ws.send_json(ack)
            _pong_next(ws)
        assert seen == [w.requester.node_id]


# ---------------------------------------------------------------------------
# A legacy hello takes the slot of a token-bound connection that holds links
# ---------------------------------------------------------------------------
#
# The newer connection still wins (that is the legacy path as it always was). The
# link side must fail closed: the coordinator no longer believes the machine can
# lend or borrow, nothing is written to the new connection, the links stay on the
# books and are treated like those of an offline sidecar, and the real sidecar is
# brought up to date when it comes back with its token.


@pytest.mark.asyncio
@pytest.mark.parametrize("victim_role", ["lender", "requester"])
async def test_a_legacy_hello_displacing_a_token_bound_connection_fails_the_link_side_closed(
    make_world, monkeypatch, victim_role
) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    victim, other = (w.lender, w.requester) if victim_role == "lender" else (w.requester, w.lender)
    auths = {m.node_id: await issue_node_token(w.factory, m.node_id) for m in (w.requester, w.lender)}
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    base = f"/me/circles/{w.circle_id}/links"

    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=auths[w.requester.node_id]) as req_ws, \
                client.websocket_connect("/mesh/signal", headers=auths[w.lender.node_id]) as len_ws:
            _hello(req_ws, w.requester.wg_key, features=[LINK])
            _hello(len_ws, w.lender.wg_key, features=[LINK])
            sockets = {w.requester.node_id: req_ws, w.lender.node_id: len_ws}
            victim_ws, other_ws = sockets[victim.node_id], sockets[other.node_id]

            # A live link, set up over the real transport.
            thread, result = _post_in_thread(
                client, "post", base,
                json={"requester_node_id": str(w.requester.node_id), "model_slug": w.model})
            to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
            _ack(req_ws, to_req, "add")
            _ack(len_ws, to_len, "add")
            thread.join(timeout=5)
            assert result["resp"].status_code == 201, result["resp"].text
            link_id = result["resp"].json()["link_id"]
            assert LINK in mesh.signal_node_features(victim.node_id)

            # Someone who knows only the machine's public key says hello as it.
            with client.websocket_connect("/mesh/signal") as legacy_ws:
                welcome = _hello(legacy_ws, victim.wg_key, features=[LINK])
                assert welcome["type"] == "welcome", "the legacy path still joins"

                # Unchanged: the newer connection won and the token-bound one was closed.
                with pytest.raises(WebSocketDisconnect):
                    victim_ws.receive_json()
                # Changed: the coordinator no longer believes this machine can link.
                assert mesh.signal_node_connected(victim.node_id)
                assert LINK not in mesh.signal_node_features(victim.node_id)
                assert (await w.row(link_id)).ended_at is None, "the link stays on the books"

                # It cannot start a link as the requester, or be chosen as a lender.
                thread, result = _post_in_thread(
                    client, "post", base,
                    json={"requester_node_id": str(w.requester.node_id), "model_slug": w.model})
                thread.join(timeout=10)
                detail = result["resp"].json()["detail"]
                want = "no_lender" if victim_role == "lender" else "requester_sidecar_unsupported"
                assert (result["resp"].status_code, detail["code"]) == (409, want)
                assert [r.id for r in await w.live()] == [uuid.UUID(link_id)], "and made no new row"

                # A renewal reaches the machine that is still link-capable and not the
                # new connection; the answer says so.
                await settle(w, link_id)
                thread, result = _post_in_thread(client, "post", f"{base}/{link_id}/renew", json={})
                extend = other_ws.receive_json()
                assert extend["type"] == "circle_peer_extend"
                _ack(other_ws, extend, "extend")
                thread.join(timeout=10)
                assert result["resp"].status_code == 200, result["resp"].text
                other_role = "requester" if victim_role == "lender" else "lender"
                assert result["resp"].json()["delivered"] == {victim_role: False, other_role: True}
                _pong_next(legacy_ws)             # no extend went to the new connection

                # Ending the link tells the other end and nobody else.
                assert client.delete(f"{base}/{link_id}").status_code == 204
                assert other_ws.receive_json() == {
                    "type": "circle_peer_remove", "link_id": link_id, "reason": "ended"}
                _pong_next(legacy_ws)             # nor a remove

                # The real sidecar comes back with its token. It takes the slot again and
                # is told, by the replay, that the link it may still be holding is over.
                with client.websocket_connect("/mesh/signal", headers=auths[victim.node_id]) as back:
                    assert _hello(back, victim.wg_key, features=[LINK])["type"] == "welcome"
                    assert LINK in mesh.signal_node_features(victim.node_id)
                    assert back.receive_json() == {
                        "type": "circle_peer_remove", "link_id": link_id, "reason": "ended"}
                    with pytest.raises(WebSocketDisconnect):
                        legacy_ws.receive_json()      # the legacy connection is the displaced one now


@pytest.mark.asyncio
async def test_the_real_sidecar_gets_its_live_link_back_when_it_returns_after_a_legacy_hello(
    make_world, monkeypatch
) -> None:
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1")
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id

    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws:
            _hello(req_ws, w.requester.wg_key, features=[LINK])
            with client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
                _hello(len_ws, w.lender.wg_key, features=[LINK])
                thread, result = _post_in_thread(
                    client, "post", f"/me/circles/{w.circle_id}/links",
                    json={"requester_node_id": str(w.requester.node_id), "model_slug": w.model})
                to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
                _ack(req_ws, to_req, "add")
                _ack(len_ws, to_len, "add")
                thread.join(timeout=5)
                link_id = result["resp"].json()["link_id"]

                with client.websocket_connect("/mesh/signal") as legacy_ws:
                    _hello(legacy_ws, w.lender.wg_key, features=[LINK])
                    _pong_next(legacy_ws)            # welcome only
                    with client.websocket_connect("/mesh/signal", headers=len_auth) as back:
                        assert _hello(back, w.lender.wg_key, features=[LINK])["type"] == "welcome"
                        add = back.receive_json()
                        assert (add["type"], add["role"], add["link_id"]) == ("circle_peer_add", "lender", link_id)
                        assert add["token_sha256"] == to_len["token_sha256"]
                        _pong_next(back)


# ---------------------------------------------------------------------------
# The send path: nothing reaches a connection that lost the feature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_send_link_writes_only_to_a_connection_that_holds_the_feature(make_world) -> None:
    w = await make_world()
    remove = {"type": "circle_peer_remove", "link_id": str(uuid.uuid4()), "reason": "ended"}
    assert await mesh.signal_send_link(w.requester.node_id, remove) is True
    assert w.requester.sidecar.sent == [remove]

    legacy = FakeSidecar(w.lender.node_id)
    mesh._signal_connections[w.lender.node_id] = legacy
    mesh._signal_features[w.lender.node_id] = frozenset({"something_newer"})
    assert await mesh.signal_send_link(w.lender.node_id, remove) is False
    assert legacy.sent == []
    # Household traffic to the same connection is untouched by the rule.
    assert await mesh.signal_send(w.lender.node_id, {"type": "pong"}) is True
    assert legacy.sent == [{"type": "pong"}]
    # No connection at all.
    assert await mesh.signal_send_link(uuid.uuid4(), remove) is False


@pytest.mark.asyncio
async def test_a_legacy_connection_taking_the_lenders_slot_mid_create_is_never_written_to(make_world) -> None:
    """The create checked the lender, then a legacy hello took its slot before the
    add went out. The check inside the send catches it."""
    w = await make_world()
    legacy = FakeSidecar(w.lender.node_id)

    def takeover(_payload: dict) -> None:
        mesh._signal_connections[w.lender.node_id] = legacy
        mesh._signal_features[w.lender.node_id] = frozenset()

    # The two adds are sent in order, the requester's first, and FakeSidecar runs
    # this hook before it returns, so the takeover lands before the lender's add.
    w.requester.sidecar.on_send = takeover
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (502, "sidecar_rejected")
    assert legacy.sent == []
    assert await w.live() == [], "a link whose lender cannot be told is not left live"
    assert [m["type"] for m in w.requester.sidecar.sent] == ["circle_peer_add", "circle_peer_remove"]


@pytest.mark.asyncio
async def test_a_legacy_connection_taking_the_requesters_slot_mid_create_is_never_written_to(
    make_world, monkeypatch
) -> None:
    """The same for the requester's end. The takeover is made to land after the
    create has checked the requester and before it sends anything."""
    w = await make_world()
    legacy = FakeSidecar(w.requester.node_id)
    real = circle_links.lender_member_ids

    async def takeover_then_look_up(*args, **kwargs):
        mesh._signal_connections[w.requester.node_id] = legacy
        mesh._signal_features[w.requester.node_id] = frozenset()
        return await real(*args, **kwargs)

    monkeypatch.setattr(circle_links, "lender_member_ids", takeover_then_look_up)
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (409, "requester_sidecar_offline")
    assert legacy.sent == []
    assert await w.live() == []
    # The lender had been sent its add before the requester's failure was known,
    # so it is told to drop it.
    assert [m["type"] for m in w.lender.sidecar.sent] == ["circle_peer_add", "circle_peer_remove"]


_ACTIONS = ["end", "renew", "sweep_expired", "lender_leaves", "sharing_off", "circle_deleted",
            "node_removed", "replay"]


@pytest.mark.asyncio
@pytest.mark.parametrize("victim_role", ["lender", "requester"])
@pytest.mark.parametrize("action", _ACTIONS)
async def test_no_link_message_is_written_to_a_connection_that_lost_the_feature(
    make_world, action, victim_role
) -> None:
    """Every way the coordinator sends a link message, with one end's slot held by
    a connection that is not token-bound. That end's socket stays empty; the other
    end is still told (so the action really ran)."""
    w = await make_world()
    out = await create(w)
    victim, other = (w.lender, w.requester) if victim_role == "lender" else (w.requester, w.lender)

    # What a legacy hello leaves behind: a connection, without the link feature.
    mesh._signal_features[victim.node_id] = frozenset({"something_newer"})
    victim.sidecar.sent.clear()
    other.sidecar.sent.clear()

    expect_other: str | None
    if action == "end":
        await end(w, out.link_id)
        expect_other = "circle_peer_remove"
    elif action == "renew":
        await settle(w, out.link_id)
        answer = await renew(w, out.link_id)
        assert getattr(answer.delivered, victim_role) is False
        assert getattr(answer.delivered, "requester" if victim_role == "lender" else "lender") is True
        expect_other = "circle_peer_extend"
    elif action == "sweep_expired":
        async with w.factory() as db:
            await db.execute(text(
                "UPDATE circle_links SET expires_at = now() - interval '1 second', "
                "created_at = now() - interval '11 minutes' WHERE id = :i"), {"i": out.link_id})
            await db.commit()
        assert (await circle_links.sweep_once(w.factory)).expired == 1
        expect_other = "circle_peer_remove"
    elif action == "lender_leaves":
        async with w.factory() as db:
            await circles_router.leave_circle(w.circle_id, db=db, member_id=w.lender.member_id)
        expect_other = "circle_peer_remove"
    elif action == "sharing_off":
        await circle_links.on_peer_registered(
            w.lender.peer_id, serves_circle=False, mesh_node_id=None, circle_port=None)
        expect_other = "circle_peer_remove"
    elif action == "circle_deleted":
        async with w.factory() as db:
            ended = await circle_links.end_for_circle(db, w.circle_id)
            await db.commit()
        await circle_links.push_removed(ended)
        expect_other = "circle_peer_remove"
    elif action == "node_removed":
        async with w.factory() as db:
            ended = await circle_links.end_for_node(db, victim.node_id)
            await db.commit()
        await circle_links.push_removed(ended)
        expect_other = "circle_peer_remove"
    else:  # replay: the victim's own reconnect hook, and the other end's
        await circle_links.replay_for_node(victim.node_id)
        expect_other = None

    assert victim.sidecar.sent == [], f"{action}: a link message went to a connection without the feature"
    if expect_other is not None:
        assert [m["type"] for m in other.sidecar.sent] == [expect_other]
    else:
        assert other.sidecar.sent == []
        await circle_links.replay_for_node(other.node_id)
        assert [m["type"] for m in other.sidecar.sent] == ["circle_peer_add"], "the other end still gets its replay"


@pytest.mark.asyncio
async def test_the_lender_hook_after_its_engine_disconnects_sends_nothing_to_a_legacy_connection(
    make_world, monkeypatch
) -> None:
    """The grace timer (lender engine gone for LENDER_DISCONNECT_GRACE_S) is the
    one send path with no request behind it."""
    monkeypatch.setattr(circle_links, "LENDER_DISCONNECT_GRACE_S", 0.2)
    w = await make_world()
    out = await create(w)
    mesh._signal_features[w.lender.node_id] = frozenset()
    w.lender.sidecar.sent.clear()
    w.requester.sidecar.sent.clear()
    from coordinator.services.peer_registry import unregister_peer
    await unregister_peer(w.lender.peer_id)
    circle_links.on_peer_gone(w.lender.peer_id, w.lender.node_id)
    assert await _wait_async(lambda: any(m["type"] == "circle_peer_remove" for m in w.requester.sidecar.sent))
    assert (await w.row(out.link_id)).end_reason == "lender_gone"
    assert w.lender.sidecar.sent == []


async def _wait_async(predicate, timeout: float = 3.0) -> bool:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# The rule has no off switch, and one door for link messages
# ---------------------------------------------------------------------------


def test_no_setting_turns_the_node_token_rule_off() -> None:
    """The rule is computed from the connection and read from nowhere else. If a
    new circle setting is added, this test fails so that someone checks it does
    not loosen the rule."""
    from coordinator.config import Settings

    assert {n for n in Settings.model_fields if n.startswith("circle_link")} == {
        "circle_links_enabled", "circle_link_direct", "circle_link_ttl_s",
        "circle_link_sweep_interval_s", "circle_link_bind_without_session",
    }
    root = Path(__file__).resolve().parent.parent / "coordinator"
    mesh_src = (root / "routers/mesh.py").read_text(encoding="utf-8")
    handler = mesh_src[mesh_src.index("async def mesh_signal("):]
    assert "token_bound = auth_node_id is not None and auth_node_id == node_id" in handler
    for reader in ("get_settings", "os.environ", "getenv", "settings."):
        assert reader not in handler, f"mesh_signal reads configuration ({reader})"
    gate = mesh_src[mesh_src.index("async def _send_to_node("):mesh_src.index("def signal_node_connected(")]
    for reader in ("get_settings", "os.environ", "getenv", "settings."):
        assert reader not in gate


def test_link_messages_have_one_door_and_it_is_the_gated_one() -> None:
    """Nothing but circle_links.py builds circle_peer_* messages, and it sends them
    only through mesh.signal_send_link (never the ungated signal_send, never a
    socket)."""
    root = Path(__file__).resolve().parent.parent / "coordinator"
    builders = {"SignalCirclePeerAdd", "SignalCirclePeerExtend", "SignalCirclePeerRemove"}
    users: set[str] = set()
    ungated: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in builders and rel != "schemas/mesh.py":
                    users.add(rel)
                if name == "signal_send":
                    ungated.append(rel)
    assert users == {"services/circle_links.py"}
    assert ungated == [], f"signal_send (no node-token gate) is called from {ungated}"
    service = (root / "services/circle_links.py").read_text(encoding="utf-8")
    assert service.count("signal_send_link(") == 1, "one send, inside _send"
