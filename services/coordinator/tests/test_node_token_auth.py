"""HTTP-level tests for the per-node bearer-token auth contract (see
`middleware/node_auth.py`).

Background: an app-spawned mesh sidecar should never need the machine-wide
`POTLUCK_COORDINATOR_TOKEN` for its own ongoing calls. A valid pairing
token is sufficient auth on its own for `POST /mesh/enroll`; on
success the response carries a fresh per-node `node_token` the sidecar then
presents as `Authorization: Bearer <node_token>` on every subsequent
machine call. The legacy `X-Potluck-Coordinator-Token` keeps working
unchanged everywhere (back-compat).

These mount the real router against the live docker-compose Postgres (same
DB every other coordinator integration test uses - see
`test_device_key_registration.py` for the `_make_app_with_db` pattern this
mirrors) so the actual FastAPI dependency graph is exercised end-to-end,
including the `/mesh/signal` WebSocket upgrade.

Coverage:
  - a valid pairing token alone (no coordinator token) enrolls successfully.
  - the enroll response carries a fresh, high-entropy, per-enrollment
    node_token.
  - a reused (already-consumed) pairing token fails closed (409), even
    when re-presented with a valid coordinator token.
  - an invalid pairing token + no coordinator token -> 401 (auth gate,
    not the token-specific 404/409/410 - those require a credential to
    even reach step 1).
  - an invalid pairing token + a valid coordinator token still surfaces
    the specific 404 (coordinator token substitutes for "pairing token
    not required", never for "any pairing token accepted").
  - node_token authorizes heartbeat / peers / derp-token / node-delete /
    the signal WS hello for its own node.
  - node_token is rejected (403, or WS "unauthorized" + close) when the
    request identifies a different node (wg_public_key/node_id mismatch).
  - the legacy coordinator token keeps authorizing every machine route
    with no per-node binding (back-compat regression guard).
  - a node_token does not double as a member session token on the
    session-authed routes (/mesh/households) - the two credential systems
    stay separated even though both ride an `Authorization: Bearer` header.
"""
from __future__ import annotations

import base64
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from coordinator.db.database import close_db, get_session_factory, init_db
from coordinator.models.household import Household
from coordinator.models.pairing import PairingToken
from coordinator.routers.mesh import router

_COORDINATOR_TOKEN = "s3cret-coordinator-token"


def _make_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        try:
            yield
        finally:
            await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app


def _random_cidr() -> str:
    # Unique /24 inside the CGNAT range households actually use, chosen
    # randomly to avoid colliding with other parallel test runs.
    b = 64 + secrets.randbelow(64)
    c = secrets.randbelow(256)
    return f"100.{b}.{c}.0/24"


def _fresh_wg_pubkey() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _pairing_token_value() -> str:
    return f"tok-{uuid.uuid4().hex[:20]}"


@pytest.fixture(autouse=True)
def _derp_jwt_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # POST /mesh/derp-token 503s without this - set it for every test in
    # this module regardless of whether the individual test exercises
    # derp-token, for uniformity with test_derp_pop.py's convention.
    monkeypatch.setenv("POTLUCK_DERP_JWT_KEY", "test-derp-jwt-key")


@pytest.fixture(autouse=True)
def _no_coordinator_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Start every test from a clean dev-mode baseline; individual tests
    # opt into a configured coordinator token via monkeypatch.setenv.
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)


@dataclass
class _Households:
    """Tracks household IDs created during a test so teardown can delete
    them - cascades to pairing_tokens + mesh_nodes via ON DELETE CASCADE,
    so a single DELETE per household leaves the DB clean."""

    ids: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture()
def client():
    app = _make_app()
    households = _Households()
    with TestClient(app, raise_server_exceptions=False) as c:
        c.households = households  # type: ignore[attr-defined]
        yield c

        async def _cleanup():
            if not households.ids:
                return
            factory = get_session_factory()
            async with factory() as db:
                async with db.begin():
                    for hid in households.ids:
                        await db.execute(
                            text("DELETE FROM households WHERE id = :h"), {"h": hid}
                        )

        c.portal.call(_cleanup)


async def _seed_household_and_pairing_token() -> tuple[uuid.UUID, str]:
    """Insert a Household + one unconsumed PairingToken directly (bypassing
    the session-authed /mesh/households + /mesh/pairing-tokens routes,
    which are unmodified by this change and covered by their own tests)."""
    factory = get_session_factory()
    member_id = uuid.uuid4()
    token_value = _pairing_token_value()
    async with factory() as db:
        async with db.begin():
            household = Household(
                owner_member_id=member_id,
                name="node-token-test",
                mesh_cidr=_random_cidr(),
            )
            db.add(household)
            await db.flush()
            household_id = household.id
            db.add(
                PairingToken(
                    token=token_value,
                    household_id=household_id,
                    issued_by_member_id=member_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
    return household_id, token_value


def _enroll(client, token_value: str, *, headers: dict[str, str] | None = None) -> dict:
    resp = client.post(
        "/mesh/enroll",
        json={
            "pairing_token": token_value,
            "wg_public_key": _fresh_wg_pubkey(),
            "node_name": f"node-{uuid.uuid4().hex[:8]}",
        },
        headers=headers or {},
    )
    return resp


# ---------------------------------------------------------------------------
# Enroll auth: pairing token alone is sufficient
# ---------------------------------------------------------------------------


def test_enroll_with_valid_pairing_token_no_coordinator_token_succeeds(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)

    resp = _enroll(client, token_value)  # no headers at all
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["household_id"] == str(household_id)
    assert uuid.UUID(body["node_id"])


def test_enroll_returns_high_entropy_node_token(client) -> None:
    household_id_a, token_value_a = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id_a)
    household_id_b, token_value_b = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id_b)

    resp_a = _enroll(client, token_value_a)
    assert resp_a.status_code == 201, resp_a.text
    token_a = resp_a.json()["node_token"]

    # >=32 raw bytes urlsafe-base64 (no padding) -> at least 43 chars.
    assert isinstance(token_a, str)
    assert len(token_a) >= 43
    assert "=" not in token_a  # no padding

    resp_b = _enroll(client, token_value_b)
    assert resp_b.status_code == 201, resp_b.text
    token_b = resp_b.json()["node_token"]

    assert token_a != token_b  # freshly minted per enrollment, never reused


def test_enroll_reused_pairing_token_fails_closed(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)

    first = _enroll(client, token_value)
    assert first.status_code == 201, first.text

    second = _enroll(client, token_value)
    assert second.status_code == 409
    assert "already consumed" in second.json()["detail"]

    # ... and still fails even presenting a valid coordinator token, proving
    # the coordinator token never substitutes for "any pairing token
    # accepted" - only for "no pairing token needed".
    third = _enroll(
        client,
        token_value,
        headers={"X-Potluck-Coordinator-Token": "irrelevant-since-unset"},
    )
    assert third.status_code == 409


def test_enroll_invalid_pairing_token_no_coordinator_token_401(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    resp = _enroll(client, "totally-bogus-pairing-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


def test_enroll_invalid_pairing_token_with_valid_coordinator_token_404(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: with the coordinator token, enroll still validates the
    pairing token and surfaces the SPECIFIC reason (404 here) rather than a
    generic 401 - unchanged from pre-node-token behaviour."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    resp = _enroll(
        client,
        "totally-bogus-pairing-token",
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "pairing token not found"


def test_enroll_dev_fleet_flow_unchanged(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinator-token path - valid coordinator token + valid pairing
    token - still succeeds."""
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)

    resp = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["node_token"]


# ---------------------------------------------------------------------------
# node_token authorizes each machine route for its OWN node
# ---------------------------------------------------------------------------


def _wg_key_of(client, enrolled: dict) -> str:
    """Fetch the wg_public_key back out of the DB for the just-enrolled
    node (the enroll response itself doesn't echo it back)."""

    async def _fetch():
        factory = get_session_factory()
        async with factory() as db:
            row = await db.execute(
                text("SELECT wg_public_key FROM mesh_nodes WHERE id = :i"),
                {"i": enrolled["node_id"]},
            )
            return row.scalar_one()

    return client.portal.call(_fetch)


def test_node_token_authorizes_heartbeat(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.post(
        "/mesh/heartbeat",
        json={"node_id": enrolled["node_id"], "wg_public_key": wg_key},
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True


def test_node_token_authorizes_peers(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.get(
        "/mesh/peers",
        params={"wg_public_key": wg_key},
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []  # sole node in the household - empty roster


def test_node_token_authorizes_derp_token(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.post(
        "/mesh/derp-token",
        json={"wg_public_key": wg_key, "region": "ord"},
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]


def test_node_token_authorizes_self_delete(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.delete(
        f"/mesh/nodes/{enrolled['node_id']}",
        params={"wg_public_key": wg_key},
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    )
    assert resp.status_code == 204


def test_node_token_authorizes_signal_hello(client) -> None:
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()
    wg_key = _wg_key_of(client, enrolled)

    with client.websocket_connect(
        "/mesh/signal",
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    ) as ws:
        ws.send_json({"type": "hello", "wg_public_key": wg_key})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"
        assert msg["node_id"] == enrolled["node_id"]


# ---------------------------------------------------------------------------
# node_token cannot act as a DIFFERENT node
# ---------------------------------------------------------------------------


def _enroll_two_nodes_same_household(client) -> tuple[dict, dict]:
    household_id, token_value_a = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    node_a = _enroll(client, token_value_a).json()

    token_value_b = _pairing_token_value()

    async def _issue_second_token():
        factory = get_session_factory()
        async with factory() as db:
            async with db.begin():
                db.add(
                    PairingToken(
                        token=token_value_b,
                        household_id=household_id,
                        issued_by_member_id=uuid.uuid4(),
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                )

    client.portal.call(_issue_second_token)
    node_b = _enroll(client, token_value_b).json()
    return node_a, node_b


def test_node_token_cannot_heartbeat_as_another_node(client) -> None:
    node_a, node_b = _enroll_two_nodes_same_household(client)
    wg_key_b = _wg_key_of(client, node_b)

    resp = client.post(
        "/mesh/heartbeat",
        json={"node_id": node_b["node_id"], "wg_public_key": wg_key_b},
        headers={"Authorization": f"Bearer {node_a['node_token']}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "node_token does not authorize this node"


def test_node_token_cannot_list_peers_as_another_node(client) -> None:
    node_a, node_b = _enroll_two_nodes_same_household(client)
    wg_key_b = _wg_key_of(client, node_b)

    resp = client.get(
        "/mesh/peers",
        params={"wg_public_key": wg_key_b},
        headers={"Authorization": f"Bearer {node_a['node_token']}"},
    )
    assert resp.status_code == 403


def test_node_token_cannot_get_derp_token_as_another_node(client) -> None:
    node_a, node_b = _enroll_two_nodes_same_household(client)
    wg_key_b = _wg_key_of(client, node_b)

    resp = client.post(
        "/mesh/derp-token",
        json={"wg_public_key": wg_key_b, "region": "ord"},
        headers={"Authorization": f"Bearer {node_a['node_token']}"},
    )
    assert resp.status_code == 403


def test_node_token_cannot_delete_claiming_another_nodes_key(client) -> None:
    node_a, node_b = _enroll_two_nodes_same_household(client)
    wg_key_b = _wg_key_of(client, node_b)

    # node_a's token, but the request claims to BE node_b (wg_public_key
    # query param names node_b) - must be rejected regardless of which
    # node is the delete TARGET.
    resp = client.delete(
        f"/mesh/nodes/{node_a['node_id']}",
        params={"wg_public_key": wg_key_b},
        headers={"Authorization": f"Bearer {node_a['node_token']}"},
    )
    assert resp.status_code == 403


def test_node_token_cannot_signal_hello_as_another_node(client) -> None:
    node_a, node_b = _enroll_two_nodes_same_household(client)
    wg_key_b = _wg_key_of(client, node_b)

    with client.websocket_connect(
        "/mesh/signal",
        headers={"Authorization": f"Bearer {node_a['node_token']}"},
    ) as ws:
        ws.send_json({"type": "hello", "wg_public_key": wg_key_b})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Coordinator-token back-compat: still authorizes every machine route,
# with no per-node binding.
# ---------------------------------------------------------------------------


def test_coordinator_token_still_authorizes_heartbeat(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    ).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.post(
        "/mesh/heartbeat",
        json={"node_id": enrolled["node_id"], "wg_public_key": wg_key},
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    )
    assert resp.status_code == 200, resp.text


def test_coordinator_token_still_authorizes_peers(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    ).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.get(
        "/mesh/peers",
        params={"wg_public_key": wg_key},
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    )
    assert resp.status_code == 200, resp.text


def test_coordinator_token_still_authorizes_derp_token(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    ).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.post(
        "/mesh/derp-token",
        json={"wg_public_key": wg_key, "region": "ord"},
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    )
    assert resp.status_code == 200, resp.text


def test_coordinator_token_still_authorizes_node_delete(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    ).json()
    wg_key = _wg_key_of(client, enrolled)

    resp = client.delete(
        f"/mesh/nodes/{enrolled['node_id']}",
        params={"wg_public_key": wg_key},
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    )
    assert resp.status_code == 204


def test_coordinator_token_still_authorizes_signal_hello(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(
        client, token_value, headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN}
    ).json()
    wg_key = _wg_key_of(client, enrolled)

    with client.websocket_connect(
        "/mesh/signal",
        headers={"X-Potluck-Coordinator-Token": _COORDINATOR_TOKEN},
    ) as ws:
        ws.send_json({"type": "hello", "wg_public_key": wg_key})
        msg = ws.receive_json()
        assert msg["type"] == "welcome"


def test_machine_routes_401_without_any_credential_when_token_configured(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _COORDINATOR_TOKEN)
    resp = client.get("/mesh/peers", params={"wg_public_key": "A" * 44})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


# ---------------------------------------------------------------------------
# node_token does not double as a member-session credential
# ---------------------------------------------------------------------------


def test_node_token_does_not_authorize_household_creation(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node_token is a machine-plumbing credential, not a member session.
    Presenting it as `Authorization: Bearer` to the session-authed
    /mesh/households route must NOT resolve to a member - proves the two
    credential systems (per-node SHA-256(node_token) vs. per-session
    SHA-256(session_token), both against different tables) stay separated
    even though both ride the same header shape."""
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    household_id, token_value = client.portal.call(_seed_household_and_pairing_token)
    client.households.ids.append(household_id)
    enrolled = _enroll(client, token_value).json()

    resp = client.post(
        "/mesh/households",
        json={"name": "should not work"},
        headers={"Authorization": f"Bearer {enrolled['node_token']}"},
    )
    assert resp.status_code == 401
