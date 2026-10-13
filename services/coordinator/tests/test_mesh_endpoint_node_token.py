"""A machine that has a node token has its endpoints written only by something
that presented that token.

A WireGuard public key is not a secret, and the legacy way of naming a node on
/mesh/signal and /mesh/heartbeat is that key. Without this rule anyone who knew a
machine's key could set the endpoint the coordinator stores for it, and under
the direct circle link policy that stored value is copied into the link message
sent to the other household's sidecar, so a third party could choose where that
sidecar sends its WireGuard handshake packets.

What these tests pin:

* every writer of last_endpoint, of the household's LAN candidates and of the
  endpoint reports the link broker reads, for both kinds of credential: the
  signal endpoint_update, POST /mesh/heartbeat and POST /mesh/enroll;
* a node enrolled without a token (no node_token_hash) keeps the legacy
  behaviour, and so does a token-bound sidecar: roster updates, punch
  coordination and LAN candidates reach its household;
* the link broker takes a machine's addresses only from the endpoint report of
  its live, token-bound connection, so an address written through any other
  path is never put in a message to another
  household, including when the real sidecar reconnects and a link is made
  before it has sent its own endpoint_update;
* a connection that another one displaced does not tell the household that the
  node left.

Sidecars here are real websockets (Starlette's test client) for the transport
tests and FakeSidecars (tests/circle_link_helpers.py) for the broker's inputs. A
node token is given to a row the way /mesh/enroll does it (issue_node_token) and
presented the way a sidecar presents it: Authorization: Bearer on the upgrade.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.websockets import WebSocketDisconnect
from structlog.testing import capture_logs

from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.models import MeshNode
from coordinator.models.pairing import PairingToken
from coordinator.routers import mesh
from coordinator.schemas.mesh import EnrollRequest
from coordinator.services import circle_links
from tests.circle_link_helpers import FakeSidecar, issue_node_token, report_endpoints, wg_key
from tests.test_circle_link_node_token import _ack, _pong_next, _post_in_thread
from tests.test_circle_link_wire import _env, _hello, _wait, make_app
from tests.test_circle_links import (  # noqa: F401  (fixtures)
    create,
    links_on,
    make_world,
    settle,
)
from tests.test_node_token_auth import (  # noqa: F401  (client is a fixture)
    _fresh_wg_pubkey,
    _seed_household_and_pairing_token,
    client as enroll_client,
)

LINK = circle_links.LINK_FEATURE
COORDINATOR_TOKEN = "shared-secret"

# What a connection or request can present that is not the node's own token. The
# first is the dev default (no coordinator token configured, so naming the node
# by its key is enough), the others are the shared token alone and with a bearer
# that matches no node.
LEGACY = ["no_credential", "coordinator_token", "unknown_bearer_and_coordinator_token"]


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("POTLUCK_CIRCLE_LINK_DIRECT", raising=False)


def _legacy_headers(how: str, monkeypatch) -> dict[str, str]:
    if how == "no_credential":
        return {}
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", COORDINATOR_TOKEN)
    if how == "coordinator_token":
        return {"X-Potluck-Coordinator-Token": COORDINATOR_TOKEN}
    return {"Authorization": "Bearer " + "y" * 43, "X-Potluck-Coordinator-Token": COORDINATOR_TOKEN}


async def _add_mate(w, *, token: bool, name: str = "mate-box", host: int = 3) -> tuple[uuid.UUID, str, dict]:
    """Another machine in the requester's household: (node id, wg key, headers)."""
    first = w.requester
    prefix = first.mesh_ip.rsplit(".", 1)[0]
    key = wg_key()
    async with w.factory() as db:
        async with db.begin():
            node = MeshNode(household_id=first.household_id, node_name=name,
                            mesh_ip=f"{prefix}.{host}", wg_public_key=key, platform="linux")
            db.add(node)
        node_id = node.id
    return node_id, key, (await issue_node_token(w.factory, node_id) if token else {})


async def _row(w, node_id: uuid.UUID) -> MeshNode:
    async with w.factory() as db:
        row = await db.get(MeshNode, node_id)
    assert row is not None
    return row


async def _store_endpoint(w, node_id: uuid.UUID, endpoint: str | None) -> None:
    async with w.factory() as db:
        await db.execute(text("UPDATE mesh_nodes SET last_endpoint = :e WHERE id = :i"),
                         {"e": endpoint, "i": node_id})
        await db.commit()


def _update(ws, endpoint: str, local: list[str] | None = None) -> None:
    ws.send_json({"type": "endpoint_update", "external_endpoint": endpoint,
                  "local_endpoints": local or []})


def _disconnected(logs, node_id: uuid.UUID, displaced: bool) -> bool:
    """The server finished with a node's connection (its last log line, after the
    decision whether to tell the household the node left)."""
    return any(e["event"] == "mesh_signal_disconnected" and e["node_id"] == str(node_id)
               and e["displaced"] is displaced for e in logs)


def _leave(ws, logs, node_id: uuid.UUID) -> None:
    """Say goodbye the way a real sidecar does and wait for the server to finish.
    Leaving a `with` instead cancels the server's handler, which skips its
    cleanup and its `removed` broadcast."""
    ws.close(1000)
    assert _wait(lambda: _disconnected(logs, node_id, displaced=False))


def _ignored(logs) -> list[dict]:
    return [e for e in logs if e["event"] == "mesh_signal_endpoint_needs_node_token"]


# ---------------------------------------------------------------------------
# Writer 1: the signal endpoint_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("how", LEGACY)
async def test_a_legacy_connection_cannot_set_the_endpoints_of_a_node_that_has_a_token(
    make_world, monkeypatch, how
) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    await issue_node_token(w.factory, x.node_id)          # x has a node token; this connection does not use it
    mate_id, mate_key, mate_auth = await _add_mate(w, token=True)
    legacy = _legacy_headers(how, monkeypatch)

    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=mate_auth) as mate_ws, \
                    client.websocket_connect("/mesh/signal", headers=legacy) as x_ws:
                assert _hello(mate_ws, mate_key)["type"] == "welcome"
                assert _hello(x_ws, x.wg_key, features=[LINK])["type"] == "welcome"
                assert [p["node_id"] for p in mate_ws.receive_json()["added"]] == [str(x.node_id)]
                seen_at_hello = (await _row(w, x.node_id)).last_seen_at

                _update(x_ws, "203.0.113.5:51820", ["192.168.1.9:51820"])
                _update(x_ws, "203.0.113.6:51820", ["192.168.1.10:51820"])
                _pong_next(x_ws)          # both were handled, and nothing was sent back
                _pong_next(mate_ws)       # nothing was passed on to the household

                row = await _row(w, x.node_id)
                assert row.last_endpoint is None
                assert row.last_seen_at == seen_at_hello, "nothing was stored, not even a sighting"
                assert x.node_id not in mesh._local_endpoints
                assert mesh.signal_link_endpoints(x.node_id) == (None, [])

    assert len(_ignored(logs)) == 1, "one line per connection, however many updates"
    assert set(_ignored(logs)[0]) <= {"event", "log_level", "node_id"}
    assert _ignored(logs)[0]["node_id"] == str(x.node_id)


@pytest.mark.asyncio
async def test_a_connection_opened_with_the_nodes_token_still_sets_its_endpoints(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    x_auth = await issue_node_token(w.factory, x.node_id)
    mate_id, mate_key, mate_auth = await _add_mate(w, token=True)

    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=mate_auth) as mate_ws, \
                    client.websocket_connect("/mesh/signal", headers=x_auth) as x_ws:
                _hello(mate_ws, mate_key)
                _hello(x_ws, x.wg_key, features=[LINK])
                mate_ws.receive_json()                                    # x joined
                _update(x_ws, "203.0.113.5:51820", ["192.168.1.9:51820", "8.8.8.8:1"])
                update = mate_ws.receive_json()
                (peer,) = update["updated"]
                assert (peer["node_id"], peer["last_endpoint"]) == (str(x.node_id), "203.0.113.5:51820")
                assert peer["local_endpoints"] == ["192.168.1.9:51820"], "re-filtered to RFC1918"
                _pong_next(x_ws)

                assert (await _row(w, x.node_id)).last_endpoint == "203.0.113.5:51820"
                assert mesh._local_endpoints[x.node_id] == ["192.168.1.9:51820"]
                assert mesh.signal_link_endpoints(x.node_id) == ("203.0.113.5:51820", ["192.168.1.9:51820"])
    assert _ignored(logs) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", LEGACY)
async def test_a_node_enrolled_before_tokens_existed_writes_its_endpoints_as_before(
    make_world, monkeypatch, how
) -> None:
    """No node_token_hash: the legacy connection is the only kind it has, and its
    endpoint_update is stored and passed on exactly as it always was. Such a node
    is never offered a link, so none of this reaches another household."""
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    mate_id, mate_key, mate_auth = await _add_mate(w, token=False)
    legacy = _legacy_headers(how, monkeypatch)
    assert (await _row(w, x.node_id)).node_token_hash is None

    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=legacy) as mate_ws, \
                    client.websocket_connect("/mesh/signal", headers=legacy) as x_ws:
                _hello(mate_ws, mate_key)
                _hello(x_ws, x.wg_key, features=[LINK])
                mate_ws.receive_json()
                _update(x_ws, "203.0.113.5:51820", ["192.168.1.9:51820"])
                (peer,) = mate_ws.receive_json()["updated"]
                assert (peer["last_endpoint"], peer["local_endpoints"]) == (
                    "203.0.113.5:51820", ["192.168.1.9:51820"])
                _pong_next(x_ws)
                assert (await _row(w, x.node_id)).last_endpoint == "203.0.113.5:51820"
                assert mesh._local_endpoints[x.node_id] == ["192.168.1.9:51820"]
                assert mesh.signal_link_endpoints(x.node_id) == (None, []), "never an input to a link"
    assert _ignored(logs) == []


@pytest.mark.asyncio
async def test_the_households_mesh_works_as_before_for_token_bound_sidecars(make_world) -> None:
    """Roster updates, punch coordination and LAN candidates, with every machine's
    connection opened with its own token."""
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    x_auth = await issue_node_token(w.factory, x.node_id)
    m1_id, m1_key, m1_auth = await _add_mate(w, token=True, name="mate-1", host=3)
    m2_id, m2_key, m2_auth = await _add_mate(w, token=True, name="mate-2", host=4)

    with capture_logs() as logs, TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=x_auth) as x_ws, \
                client.websocket_connect("/mesh/signal", headers=m1_auth) as m1_ws:
            _hello(x_ws, x.wg_key, features=[LINK])
            _hello(m1_ws, m1_key)
            x_ws.receive_json()                                           # m1 joined

            _update(x_ws, "203.0.113.5:51820", ["192.168.1.9:51820"])
            (peer,) = m1_ws.receive_json()["updated"]                     # reaches the household
            assert (peer["node_id"], peer["last_endpoint"], peer["local_endpoints"]) == (
                str(x.node_id), "203.0.113.5:51820", ["192.168.1.9:51820"])
            _update(m1_ws, "203.0.113.5:51999", ["192.168.1.20:51999"])
            (peer,) = x_ws.receive_json()["updated"]
            assert peer["last_endpoint"] == "203.0.113.5:51999"

            # A machine that joins later is welcomed with both, LAN candidates included.
            with client.websocket_connect("/mesh/signal", headers=m2_auth) as m2_ws:
                welcome = _hello(m2_ws, m2_key)
                by_node = {p["node_id"]: p for p in welcome["roster"]}
                assert by_node[str(x.node_id)]["local_endpoints"] == ["192.168.1.9:51820"]
                assert by_node[str(m1_id)]["last_endpoint"] == "203.0.113.5:51999"
                x_ws.receive_json()                                       # m2 joined
                m1_ws.receive_json()
                _leave(m2_ws, logs, m2_id)                                # and leaves again
            assert x_ws.receive_json()["removed"] == [str(m2_id)]
            assert m1_ws.receive_json()["removed"] == [str(m2_id)]

            # Punch coordination hands each side the other's stored endpoint.
            x_ws.send_json({"type": "request_punch", "target_node_id": str(m1_id)})
            initiator, responder = x_ws.receive_json(), m1_ws.receive_json()
            assert (initiator["role"], initiator["peer_endpoint"]) == ("initiator", "203.0.113.5:51999")
            assert (responder["role"], responder["peer_endpoint"]) == ("responder", "203.0.113.5:51820")


@pytest.mark.asyncio
async def test_a_pre_token_household_still_punches_and_shares_lan_candidates(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    m1_id, m1_key, _ = await _add_mate(w, token=False)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal") as x_ws, client.websocket_connect("/mesh/signal") as m1_ws:
            _hello(x_ws, x.wg_key)
            _hello(m1_ws, m1_key)
            x_ws.receive_json()
            _update(x_ws, "203.0.113.5:51820", ["192.168.1.9:51820"])
            _update(m1_ws, "203.0.113.5:51999", ["192.168.1.20:51999"])
            (seen_by_m1,) = m1_ws.receive_json()["updated"]
            (seen_by_x,) = x_ws.receive_json()["updated"]
            assert seen_by_m1["local_endpoints"] == ["192.168.1.9:51820"]
            assert seen_by_x["local_endpoints"] == ["192.168.1.20:51999"]
            x_ws.send_json({"type": "request_punch", "target_node_id": str(m1_id)})
            assert x_ws.receive_json()["peer_endpoint"] == "203.0.113.5:51999"
            assert m1_ws.receive_json()["peer_endpoint"] == "203.0.113.5:51820"


@pytest.mark.asyncio
async def test_a_legacy_connection_displacing_the_real_sidecar_cannot_overwrite_what_it_reported(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    x_auth = await issue_node_token(w.factory, x.node_id)
    with TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=x_auth) as real:
            _hello(real, x.wg_key, features=[LINK])
            _update(real, "203.0.113.5:51820", ["192.168.1.9:51820"])
            _pong_next(real)
            assert mesh.signal_link_endpoints(x.node_id) == ("203.0.113.5:51820", ["192.168.1.9:51820"])

            with client.websocket_connect("/mesh/signal") as impostor:
                assert _hello(impostor, x.wg_key, features=[LINK])["type"] == "welcome"
                _update(impostor, "198.51.100.66:51820", ["192.168.1.66:51820"])
                _pong_next(impostor)
                row = await _row(w, x.node_id)
                assert row.last_endpoint == "203.0.113.5:51820", "the sidecar's own value stands"
                assert mesh._local_endpoints[x.node_id] == ["192.168.1.9:51820"]
                # And the report was the dead connection's: nothing is offered to a link.
                assert mesh.signal_link_endpoints(x.node_id) == (None, [])


# ---------------------------------------------------------------------------
# Writer 2: POST /mesh/heartbeat
# ---------------------------------------------------------------------------


def _heartbeat(client, machine, endpoint: str | None, headers: dict | None = None):
    body = {"node_id": str(machine.node_id), "wg_public_key": machine.wg_key}
    if endpoint is not None:
        body["external_endpoint"] = endpoint
    return client.post("/mesh/heartbeat", json=body, headers=headers or {})


@pytest.mark.asyncio
async def test_a_heartbeat_with_the_nodes_own_token_sets_its_endpoint(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    auth = await issue_node_token(w.factory, w.requester.node_id)
    with TestClient(make_app()) as client:
        resp = _heartbeat(client, w.requester, "203.0.113.5:51820", auth)
        assert (resp.status_code, resp.json()["accepted"]) == (200, True)
        assert (await _row(w, w.requester.node_id)).last_endpoint == "203.0.113.5:51820"
        # A heartbeat with no endpoint clears it, as it always did: it is the machine's to clear.
        assert _heartbeat(client, w.requester, None, auth).status_code == 200
        assert (await _row(w, w.requester.node_id)).last_endpoint is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["203.0.113.66:51820", None])
@pytest.mark.parametrize("how", LEGACY)
async def test_a_heartbeat_with_a_legacy_credential_leaves_the_endpoint_of_a_node_that_has_a_token(
    make_world, monkeypatch, how, sent
) -> None:
    """The request still succeeds and still counts as a sighting. The endpoint in
    it, or its absence (which would clear the stored one), is dropped."""
    w = await make_world(requester_connected=False, lenders=0)
    auth = await issue_node_token(w.factory, w.requester.node_id)
    legacy = _legacy_headers(how, monkeypatch)
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            assert _heartbeat(client, w.requester, "203.0.113.5:51820", auth).status_code == 200
            before = await _row(w, w.requester.node_id)

            resp = _heartbeat(client, w.requester, sent, legacy)
            assert (resp.status_code, resp.json()["accepted"]) == (200, True)
            after = await _row(w, w.requester.node_id)
    assert after.last_endpoint == "203.0.113.5:51820"
    assert after.last_seen_at > before.last_seen_at, "everything else the heartbeat does still happens"
    (line,) = [e for e in logs if e["event"] == "mesh_heartbeat_endpoint_needs_node_token"]
    assert set(line) <= {"event", "log_level", "node_id"}
    assert line["node_id"] == str(w.requester.node_id)


@pytest.mark.asyncio
async def test_the_node_token_wins_when_a_heartbeat_sends_the_coordinator_token_too(make_world, monkeypatch) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", COORDINATOR_TOKEN)
    w = await make_world(requester_connected=False, lenders=0)
    auth = await issue_node_token(w.factory, w.requester.node_id)
    with TestClient(make_app()) as client:
        resp = _heartbeat(client, w.requester, "203.0.113.5:51820",
                          {**auth, "X-Potluck-Coordinator-Token": COORDINATOR_TOKEN})
        assert resp.status_code == 200
    assert (await _row(w, w.requester.node_id)).last_endpoint == "203.0.113.5:51820"


@pytest.mark.asyncio
async def test_another_nodes_token_cannot_set_the_endpoint_either(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    mate_id, mate_key, mate_auth = await _add_mate(w, token=True)
    await issue_node_token(w.factory, w.requester.node_id)
    with TestClient(make_app()) as client:
        resp = _heartbeat(client, w.requester, "203.0.113.66:51820", mate_auth)
        assert resp.status_code == 403
    assert (await _row(w, w.requester.node_id)).last_endpoint is None


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["no_credential", "coordinator_token"])
async def test_a_heartbeat_for_a_node_enrolled_before_tokens_existed_works_as_before(
    make_world, monkeypatch, how
) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    assert (await _row(w, w.requester.node_id)).node_token_hash is None
    legacy = _legacy_headers(how, monkeypatch)
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            assert _heartbeat(client, w.requester, "203.0.113.5:51820", legacy).status_code == 200
            assert (await _row(w, w.requester.node_id)).last_endpoint == "203.0.113.5:51820"
            assert _heartbeat(client, w.requester, None, legacy).status_code == 200
            assert (await _row(w, w.requester.node_id)).last_endpoint is None
    assert [e for e in logs if e["event"] == "mesh_heartbeat_endpoint_needs_node_token"] == []


# ---------------------------------------------------------------------------
# Writer 3: POST /mesh/enroll
# ---------------------------------------------------------------------------


def _fetch_endpoint(client, node_id: str):
    async def _read():
        from coordinator.db.database import get_session_factory

        async with get_session_factory()() as db:
            return (await db.execute(
                text("SELECT last_endpoint, node_token_hash FROM mesh_nodes WHERE id = :i"), {"i": node_id}
            )).one()

    return client.portal.call(_read)


def _second_pairing_token(client, household_id: uuid.UUID) -> str:
    async def _issue():
        from coordinator.db.database import get_session_factory

        value = f"tok-{uuid.uuid4().hex[:20]}"
        async with get_session_factory()() as db:
            async with db.begin():
                db.add(PairingToken(token=value, household_id=household_id, issued_by_member_id=uuid.uuid4(),
                                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        return value

    return client.portal.call(_issue)


def test_enrolling_cannot_set_an_endpoint_and_cannot_touch_one_already_enrolled(enroll_client) -> None:
    client = enroll_client
    household_id, pairing = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)

    # The request has no field that carries an endpoint, and extra fields are dropped.
    assert not ({"external_endpoint", "last_endpoint", "local_endpoints"} & set(EnrollRequest.model_fields))
    key = _fresh_wg_pubkey()
    resp = client.post("/mesh/enroll", json={
        "pairing_token": pairing, "wg_public_key": key, "node_name": "first-box",
        "external_endpoint": "198.51.100.66:51820", "last_endpoint": "198.51.100.66:51820",
        "local_endpoints": ["192.168.1.66:51820"]})
    assert resp.status_code == 201, resp.text
    enrolled = resp.json()
    last_endpoint, hash_before = _fetch_endpoint(client, enrolled["node_id"])
    assert last_endpoint is None and hash_before is not None

    # The machine sets its own, with its token.
    assert client.post("/mesh/heartbeat", headers={"Authorization": f"Bearer {enrolled['node_token']}"}, json={
        "node_id": enrolled["node_id"], "wg_public_key": key,
        "external_endpoint": "203.0.113.5:51820"}).status_code == 200

    # Enrolling the same key again, with a fresh pairing token (the re-pair case), is refused
    # before anything is written: the row, its endpoint and its token are as they were.
    again = client.post("/mesh/enroll", json={
        "pairing_token": _second_pairing_token(client, household_id), "wg_public_key": key,
        "node_name": "second-box", "external_endpoint": "198.51.100.66:51820"})
    assert again.status_code == 409
    assert "already enrolled" in again.json()["detail"]
    assert _fetch_endpoint(client, enrolled["node_id"]) == ("203.0.113.5:51820", hash_before)


# ---------------------------------------------------------------------------
# The link broker: an address reaches another household only from the live,
# token-bound connection's own report
# ---------------------------------------------------------------------------


def test_signal_link_endpoints_returns_only_the_live_connections_own_report() -> None:
    node = uuid.uuid4()
    live, other = FakeSidecar(node), FakeSidecar(node)
    try:
        assert mesh.signal_link_endpoints(node) == (None, [])
        mesh._signal_connections[node] = live      # type: ignore[assignment]
        assert mesh.signal_link_endpoints(node) == (None, []), "connected, nothing reported"
        mesh._endpoint_reports[node] = mesh._EndpointReport(
            ws=live, external_endpoint="203.0.113.5:51820", local_endpoints=("192.168.1.9:51820",))  # type: ignore[arg-type]
        assert mesh.signal_link_endpoints(node) == ("203.0.113.5:51820", ["192.168.1.9:51820"])
        mesh._signal_connections[node] = other     # type: ignore[assignment]
        assert mesh.signal_link_endpoints(node) == (None, []), "another connection took the slot"
        mesh._signal_connections.pop(node)
        assert mesh.signal_link_endpoints(node) == (None, []), "no connection"
    finally:
        mesh._signal_connections.pop(node, None)
        mesh._endpoint_reports.pop(node, None)


def _direct(monkeypatch) -> None:
    _env(monkeypatch, CIRCLE_LINKS_ENABLED="1", CIRCLE_LINK_DIRECT="1")


def _adds(w):
    (lender,), (requester,) = w.lender.sidecar.of_type("circle_peer_add"), w.requester.sidecar.of_type("circle_peer_add")
    return lender, requester


@pytest.mark.asyncio
async def test_the_direct_policy_ignores_everything_stored_that_the_live_connection_did_not_report(
    make_world, monkeypatch
) -> None:
    """A stored row and LAN candidates for both machines, sitting behind one public
    address (so the LAN candidates would be eligible), and no
    report: nothing is put in either message."""
    _direct(monkeypatch)
    w = await make_world()
    await _store_endpoint(w, w.lender.node_id, "198.51.100.66:51820")
    await _store_endpoint(w, w.requester.node_id, "198.51.100.66:40000")
    mesh._local_endpoints[w.lender.node_id] = ["192.168.1.66:51820"]
    mesh._local_endpoints[w.requester.node_id] = ["192.168.1.40:51820"]
    await create(w)
    for msg in _adds(w):
        assert msg["direct_window_ms"] == 3000
        assert msg["peer_endpoint"] is None and msg["peer_local_endpoints"] == []
    blob = json.dumps(w.lender.sidecar.sent + w.requester.sidecar.sent)
    assert "198.51.100" not in blob and "192.168.1" not in blob


@pytest.mark.asyncio
async def test_a_report_made_by_a_connection_that_is_no_longer_live_is_not_sent(make_world, monkeypatch) -> None:
    _direct(monkeypatch)
    w = await make_world()
    report_endpoints(w.lender, "198.51.100.66:51820", ["192.168.1.66:51820"])
    report_endpoints(w.requester, "198.51.100.66:40000", ["192.168.1.40:51820"])
    # The lender's sidecar reconnects: a new connection holds the slot and has said nothing yet.
    replacement = FakeSidecar(w.lender.node_id)
    mesh._signal_connections[w.lender.node_id] = replacement      # type: ignore[assignment]
    mesh._signal_features[w.lender.node_id] = frozenset({LINK})
    await create(w)
    (add,) = replacement.of_type("circle_peer_add")
    (to_requester,) = w.requester.sidecar.of_type("circle_peer_add")
    assert to_requester["peer_endpoint"] is None and to_requester["peer_local_endpoints"] == []
    # The lender's own message may name the requester, which is live and has reported; no LAN
    # candidate, because the lender has no live report to compare public addresses with.
    assert add["peer_endpoint"] == "198.51.100.66:40000"
    assert add["peer_local_endpoints"] == []


@pytest.mark.asyncio
async def test_a_stored_endpoint_is_never_sent_to_the_other_household_over_real_connections(
    make_world, monkeypatch
) -> None:
    """The lender's stored endpoint and LAN candidates predate its connection: a
    row written by a heartbeat (with the node's own token, which is allowed, but
    not said by the connection a link would use) and candidates left in memory.
    Both machines are token-bound and a link is made before either has sent an
    endpoint_update. Then each reports, and a renewal carries what they said."""
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _direct(monkeypatch)
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    base = f"/me/circles/{w.circle_id}/links"
    planted = "198.51.100.66:51820"
    mesh._local_endpoints[w.lender.node_id] = ["192.168.1.66:51820"]
    await _store_endpoint(w, w.requester.node_id, "198.51.100.66:40000")

    with TestClient(app) as client:
        assert _heartbeat(client, w.lender, planted, len_auth).status_code == 200
        assert (await _row(w, w.lender.node_id)).last_endpoint == planted

        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws, \
                client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
            _hello(req_ws, w.requester.wg_key, features=[LINK])
            _hello(len_ws, w.lender.wg_key, features=[LINK])
            body = {"requester_node_id": str(w.requester.node_id), "model_slug": w.model}
            thread, result = _post_in_thread(client, "post", base, json=body)
            to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
            for add in (to_req, to_len):
                assert add["type"] == "circle_peer_add"
                assert add["peer_endpoint"] is None and add["peer_local_endpoints"] == []
                assert add["direct_window_ms"] == 3000
            _ack(req_ws, to_req, "add")
            _ack(len_ws, to_len, "add")
            thread.join(timeout=5)
            assert result["resp"].status_code == 201, result["resp"].text
            link_id = result["resp"].json()["link_id"]
            assert "198.51.100" not in json.dumps([to_req, to_len]) and "192.168.1" not in json.dumps([to_req, to_len])

            # Each sidecar says its own address, as it does right after a welcome.
            _update(req_ws, "203.0.113.20:41000", ["192.168.1.20:41000"])
            _update(len_ws, "203.0.113.30:42000", ["192.168.1.30:42000"])
            _pong_next(req_ws)
            _pong_next(len_ws)
            await settle(w, link_id)
            thread, result = _post_in_thread(client, "post", f"{base}/{link_id}/renew", json={})
            ext_req, ext_len = req_ws.receive_json(), len_ws.receive_json()
            assert (ext_req["type"], ext_req["peer_endpoint"]) == ("circle_peer_extend", "203.0.113.30:42000")
            assert (ext_len["type"], ext_len["peer_endpoint"]) == ("circle_peer_extend", "203.0.113.20:41000")
            _ack(req_ws, ext_req, "extend")
            _ack(len_ws, ext_len, "extend")
            thread.join(timeout=5)
            assert result["resp"].status_code == 200, result["resp"].text
            assert planted not in json.dumps([ext_req, ext_len])


@pytest.mark.asyncio
async def test_a_legacy_write_then_the_real_sidecar_then_a_link_before_its_endpoint_update(
    make_world, monkeypatch
) -> None:
    """A legacy connection for the lender writes
    an endpoint and LAN candidates; the lender's real sidecar connects with its
    token and takes the slot; a link is made before that sidecar has said anything.
    The legacy write is dropped, and even a value that had been stored is not what
    the link uses. Then the real sidecar reports and a second link carries that."""
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _direct(monkeypatch)
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    base = f"/me/circles/{w.circle_id}/links"
    body = {"requester_node_id": str(w.requester.node_id), "model_slug": w.model}
    planted, planted_lan = "198.51.100.66:51820", "192.168.1.66:51820"
    frames: list[dict] = []

    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws:
            _hello(req_ws, w.requester.wg_key, features=[LINK])
            _update(req_ws, "198.51.100.66:40000", ["192.168.1.40:51820"])    # same public address
            _pong_next(req_ws)

            with client.websocket_connect("/mesh/signal") as impostor:
                assert _hello(impostor, w.lender.wg_key, features=[LINK])["type"] == "welcome"
                _update(impostor, planted, [planted_lan])
                _pong_next(impostor)
                assert (await _row(w, w.lender.node_id)).last_endpoint is None
                assert w.lender.node_id not in mesh._local_endpoints

                # The real sidecar comes up with its token and takes the slot.
                with client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
                    assert _hello(len_ws, w.lender.wg_key, features=[LINK])["type"] == "welcome"
                    with pytest.raises(WebSocketDisconnect):
                        impostor.receive_json()
                    assert LINK in mesh.signal_node_features(w.lender.node_id)

                    # Whatever a legacy path stored earlier:
                    await _store_endpoint(w, w.lender.node_id, planted)
                    mesh._local_endpoints[w.lender.node_id] = [planted_lan]

                    thread, result = _post_in_thread(client, "post", base, json=body)
                    to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
                    frames += [to_req, to_len]
                    for add in (to_req, to_len):
                        assert add["type"] == "circle_peer_add"
                        assert add["peer_local_endpoints"] == []
                    assert to_req["peer_endpoint"] is None, "the lender has said nothing on this connection"
                    assert to_len["peer_endpoint"] == "198.51.100.66:40000", "the requester's own report"
                    _ack(req_ws, to_req, "add")
                    _ack(len_ws, to_len, "add")
                    thread.join(timeout=5)
                    assert result["resp"].status_code == 201, result["resp"].text

                    # Now the real sidecar reports (behind the requester's public address) and a
                    # second link is told exactly that, LAN candidate included.
                    _update(len_ws, "198.51.100.66:51999", ["192.168.1.30:51999"])
                    _pong_next(len_ws)
                    thread, result = _post_in_thread(client, "post", base, json=body)
                    to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
                    assert to_req["type"] == to_len["type"] == "circle_peer_add"
                    frames += [to_req, to_len]
                    assert (to_req["peer_endpoint"], to_req["peer_local_endpoints"]) == (
                        "198.51.100.66:51999", ["192.168.1.30:51999"])
                    assert (to_len["peer_endpoint"], to_len["peer_local_endpoints"]) == (
                        "198.51.100.66:40000", ["192.168.1.40:51820"])
                    _ack(req_ws, to_req, "add")
                    _ack(len_ws, to_len, "add")
                    thread.join(timeout=5)
                    assert result["resp"].status_code == 201, result["resp"].text
    assert planted not in json.dumps(frames) and planted_lan not in json.dumps(frames)


@pytest.mark.asyncio
async def test_a_replay_to_a_reconnected_sidecar_carries_only_what_the_peer_reported(make_world, monkeypatch) -> None:
    """The sidecar of a live link reconnects. Its replay is built before it has
    sent its own endpoint_update, so it names the peer's reported address and
    carries no LAN candidate; a value stored for either machine plays no part."""
    w = await make_world(requester_connected=False, lender_kwargs={"connected": False})
    monkeypatch.setattr(circle_links, "ACK_TIMEOUT_S", 2.0)
    _direct(monkeypatch)
    req_auth = await issue_node_token(w.factory, w.requester.node_id)
    len_auth = await issue_node_token(w.factory, w.lender.node_id)
    app = make_app()
    app.dependency_overrides[resolve_session_member_id] = lambda: w.member_id
    body = {"requester_node_id": str(w.requester.node_id), "model_slug": w.model}

    with TestClient(app) as client:
        with client.websocket_connect("/mesh/signal", headers=req_auth) as req_ws:
            _hello(req_ws, w.requester.wg_key, features=[LINK])
            _update(req_ws, "203.0.113.20:41000", ["192.168.1.20:41000"])
            _pong_next(req_ws)
            with client.websocket_connect("/mesh/signal", headers=len_auth) as len_ws:
                _hello(len_ws, w.lender.wg_key, features=[LINK])
                thread, result = _post_in_thread(client, "post", f"/me/circles/{w.circle_id}/links", json=body)
                to_req, to_len = req_ws.receive_json(), len_ws.receive_json()
                _ack(req_ws, to_req, "add")
                _ack(len_ws, to_len, "add")
                thread.join(timeout=5)
                assert result["resp"].status_code == 201, result["resp"].text

            # The lender's row now holds an address, as if written by a legacy path before.
            await _store_endpoint(w, w.lender.node_id, "198.51.100.66:51820")
            mesh._local_endpoints[w.lender.node_id] = ["192.168.1.66:51820"]
            with client.websocket_connect("/mesh/signal", headers=len_auth) as back:
                assert _hello(back, w.lender.wg_key, features=[LINK])["type"] == "welcome"
                replay = back.receive_json()
                assert (replay["type"], replay["role"]) == ("circle_peer_add", "lender")
                assert replay["peer_endpoint"] == "203.0.113.20:41000"
                assert replay["peer_local_endpoints"] == []
                assert "198.51.100.66" not in json.dumps(replay)


@pytest.mark.asyncio
async def test_an_endpoint_report_ends_with_its_connection(make_world) -> None:
    """Connections here say goodbye (close) and let the server finish; leaving a
    `with` would cancel the handler before its cleanup."""
    w = await make_world(requester_connected=False, lenders=0)
    x_auth = await issue_node_token(w.factory, w.requester.node_id)
    node = w.requester.node_id
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=x_auth) as first:
                _hello(first, w.requester.wg_key, features=[LINK])
                _update(first, "203.0.113.5:51820")
                _pong_next(first)
                assert node in mesh._endpoint_reports
                # A new connection for the node starts with no report.
                with client.websocket_connect("/mesh/signal", headers=x_auth) as second:
                    _hello(second, w.requester.wg_key, features=[LINK])
                    _pong_next(second)
                    assert node not in mesh._endpoint_reports
                    _update(second, "203.0.113.6:51820")
                    _pong_next(second)
                    assert mesh.signal_link_endpoints(node) == ("203.0.113.6:51820", [])

                    # The displaced connection finishing takes nothing of the live one's.
                    with pytest.raises(WebSocketDisconnect):
                        first.receive_json()
                    first.close(1000)
                    assert _wait(lambda: _disconnected(logs, node, displaced=True))
                    assert mesh.signal_node_connected(node)
                    assert mesh.signal_link_endpoints(node) == ("203.0.113.6:51820", [])

                    # The live one going takes its report with it.
                    _leave(second, logs, node)
                    assert node not in mesh._endpoint_reports
                    assert not mesh.signal_node_connected(node)
                    assert mesh.signal_link_endpoints(node) == (None, [])


# ---------------------------------------------------------------------------
# Every writer, by name
# ---------------------------------------------------------------------------


def _writers() -> dict[str, set[tuple[str, str]]]:
    """Where in the package each kind of endpoint write is made, as (file, function)."""
    root = Path(__file__).resolve().parent.parent / "coordinator"
    found: dict[str, set[tuple[str, str]]] = {
        "last_endpoint": set(), "local_endpoints": set(), "report": set(), "raw_sql": set()}
    for path in root.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, fn: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
            here = (rel, fn)
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "last_endpoint":
                    found["last_endpoint"].add(here)
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                    if t.value.id == "_local_endpoints":
                        found["local_endpoints"].add(here)
                    if t.value.id == "_endpoint_reports":
                        found["report"].add(here)
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name == "setattr" and len(node.args) > 1 and getattr(node.args[1], "value", None) == "last_endpoint":
                    found["last_endpoint"].add(here)
                if name != "EnrolledPeer" and any(k.arg == "last_endpoint" for k in node.keywords):
                    found["last_endpoint"].add(here)
                if name == "_EndpointReport":
                    found["report"].add(here)
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    if f.value.id == "_local_endpoints" and f.attr in ("update", "setdefault", "pop", "clear", "popitem"):
                        found["local_endpoints"].add(here)
                    if f.value.id == "_endpoint_reports" and f.attr in ("update", "setdefault", "pop", "clear", "popitem"):
                        found["report"].add(here)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"\b(update|insert\s+into)\s+(only\s+)?mesh_nodes\b", node.value, re.I):
                    found["raw_sql"].add(here)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("update", "insert") and node.args:
                first = node.args[0]
                if "MeshNode" in ast.dump(first):
                    found["raw_sql"].add(here)
            for child in ast.iter_child_nodes(node):
                visit(child, fn)

        visit(tree, "<module>")
    return found


def test_every_writer_of_an_endpoint_is_one_of_the_gated_ones() -> None:
    """If a new route or message starts writing a node's endpoint, this fails so
    that someone gives it the node-token rule (and adds it to the README list)."""
    found = _writers()
    gated = {("routers/mesh.py", "heartbeat"), ("routers/mesh.py", "mesh_signal")}
    assert found["last_endpoint"] == gated
    assert found["local_endpoints"] == {("routers/mesh.py", "mesh_signal")}
    assert found["report"] == {("routers/mesh.py", "mesh_signal")}
    assert found["raw_sql"] == set()


def test_the_endpoint_report_is_made_in_one_place_and_only_for_a_token_bound_live_connection() -> None:
    root = Path(__file__).resolve().parent.parent / "coordinator"
    src = (root / "routers/mesh.py").read_text(encoding="utf-8")
    handler = src[src.index("async def mesh_signal("):]
    assert handler.count("_EndpointReport(") == 1
    at = handler.index("_EndpointReport(")
    guard = handler[handler.rindex("if ", 0, at):].splitlines()[0].strip()
    assert guard == "if token_bound and _signal_connections.get(node_id) is ws:"
    # The broker reads the report and nothing else.
    service = (root / "services/circle_links.py").read_text(encoding="utf-8")
    assert "last_endpoint" not in service.replace("MeshNode.last_endpoint", "")
    assert "signal_local_endpoints" not in service
    assert not re.search(r"(?<![A-Za-z0-9])_local_endpoints", service), "the household's LAN candidates"
    assert service.count("signal_link_endpoints(") == 2, "own and other, in _endpoints_for only"


# ---------------------------------------------------------------------------
# A displaced connection does not say the node left
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", ["token_bound", "legacy"])
async def test_a_displaced_connection_does_not_tell_the_household_the_node_left(make_world, newer) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    x_auth = await issue_node_token(w.factory, x.node_id)
    mate_id, mate_key, mate_auth = await _add_mate(w, token=True)
    with capture_logs() as logs:
        with TestClient(make_app()) as client:
            with client.websocket_connect("/mesh/signal", headers=mate_auth) as mate_ws:
                assert _hello(mate_ws, mate_key)["type"] == "welcome"
                with client.websocket_connect("/mesh/signal", headers=x_auth) as first:
                    assert _hello(first, x.wg_key)["type"] == "welcome"
                    assert [p["node_id"] for p in mate_ws.receive_json()["added"]] == [str(x.node_id)]

                    with client.websocket_connect(
                        "/mesh/signal", headers=x_auth if newer == "token_bound" else {}
                    ) as second:
                        assert _hello(second, x.wg_key)["type"] == "welcome"
                        # A rejoin is still announced as an add, as it always was.
                        assert [p["node_id"] for p in mate_ws.receive_json()["added"]] == [str(x.node_id)]
                        with pytest.raises(WebSocketDisconnect):
                            first.receive_json()              # the displaced one was closed, as before
                        first.close(1000)                     # and it finishes, as a real client's would
                        assert _wait(lambda: _disconnected(logs, x.node_id, displaced=True))
                        _pong_next(mate_ws)                   # and no `removed` followed
                        assert mesh.signal_node_connected(x.node_id)

                        # The connection that holds the slot going is the node leaving.
                        _leave(second, logs, x.node_id)
                        update = mate_ws.receive_json()
                        assert update["type"] == "roster_update" and update["removed"] == [str(x.node_id)]


@pytest.mark.asyncio
async def test_a_connection_that_is_not_displaced_still_says_the_node_left(make_world) -> None:
    w = await make_world(requester_connected=False, lenders=0)
    x = w.requester
    mate_id, mate_key, mate_auth = await _add_mate(w, token=True)
    with capture_logs() as logs, TestClient(make_app()) as client:
        with client.websocket_connect("/mesh/signal", headers=mate_auth) as mate_ws:
            _hello(mate_ws, mate_key)
            with client.websocket_connect("/mesh/signal") as x_ws:
                _hello(x_ws, x.wg_key)
                mate_ws.receive_json()
                _leave(x_ws, logs, x.node_id)
            update = mate_ws.receive_json()
            assert update["removed"] == [str(x.node_id)]
