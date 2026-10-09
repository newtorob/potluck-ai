"""Direct circle links: create, renew and end, against real rows.

The mesh sidecars are FakeSidecars (tests/circle_link_helpers.py), so the real
service code runs: lender choice through select_peer_for_request, the token,
the supersede rule, the acknowledgements and their 2 second wait, the limits,
and the error codes. Revocation, the sweeper and the websocket transport are in
test_circle_link_revocation.py and test_circle_link_wire.py.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from coordinator.config import get_settings
from coordinator.models import CircleMembership, MemberKey, Peer, PeerCapability
from coordinator.routers import mesh
from coordinator.schemas.circle_link import CircleLinkCreateRequest
from coordinator.schemas.mesh import (
    SignalCirclePeerAdd,
    SignalCirclePeerExtend,
    SignalCirclePeerRemove,
)
from coordinator.services import circle_links
from coordinator.services.circles import lender_member_ids
from coordinator.services.peer_registry import (
    PeerConnection,
    register_peer,
    select_peer_for_request,
    unregister_peer,
)
from tests.circle_link_helpers import FakeSidecar, _WS, build_world, raw_dump, wg_key


@pytest.fixture()
def links_on(monkeypatch):
    monkeypatch.setenv("POTLUCK_CIRCLE_LINKS_ENABLED", "1")
    monkeypatch.delenv("POTLUCK_CIRCLE_LINK_DIRECT", raising=False)
    monkeypatch.delenv("POTLUCK_CIRCLE_LINK_TTL_S", raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest_asyncio.fixture()
async def make_world(session_factory, links_on):
    """Build worlds; everything they created is removed afterwards."""
    from coordinator.db import database

    links_on.setattr(database, "_session_factory", session_factory.factory)
    links_on.setattr(circle_links, "_PROCESS_STARTED", time.monotonic() - 3600)
    # Long enough that a busy machine does not turn a working ack into a timeout;
    # the tests that want a timeout wait it out (about a second).
    links_on.setattr(circle_links, "ACK_TIMEOUT_S", 1.0)
    circle_links._create_attempts.clear()
    worlds = []

    async def make(**kw):
        world = await build_world(session_factory.factory, **kw)
        worlds.append(world)
        return world

    yield make
    for world in worlds:
        await world.close()
    circle_links._create_attempts.clear()
    circle_links._create_locks.clear()


async def create(w, *, member=None, node=None, model=None, exclude=(), circle=None):
    async with w.factory() as db:
        return await circle_links.create_link(
            db,
            member_id=member or w.member_id,
            circle_id=circle or w.circle_id,
            req=CircleLinkCreateRequest(
                requester_node_id=node or w.requester.node_id,
                model_slug=model or w.model,
                exclude_link_ids=list(exclude),
            ),
        )


async def renew(w, link_id, *, member=None, circle=None):
    async with w.factory() as db:
        return await circle_links.renew_link(
            db, member_id=member or w.member_id, circle_id=circle or w.circle_id, link_id=link_id
        )


async def end(w, link_id, *, member=None, circle=None):
    async with w.factory() as db:
        await circle_links.end_link(
            db, member_id=member or w.member_id, circle_id=circle or w.circle_id, link_id=link_id
        )


async def settle(w, link_id, seconds: int = 300):
    """Make the link look `seconds` old, so a renewal is not too soon after its creation."""
    async with w.factory() as db:
        await db.execute(
            text("UPDATE circle_links SET created_at = created_at - make_interval(secs => :s) WHERE id = :i"),
            {"s": seconds, "i": link_id},
        )
        await db.commit()


async def refused(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await coro
    return exc.value


def code_of(exc: HTTPException) -> str:
    return exc.detail["code"]


# ---------------------------------------------------------------------------
# A create that works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_makes_a_link_and_tells_both_sidecars(make_world) -> None:
    w = await make_world()
    out = await create(w)

    assert out.ready is True and out.circle_id == w.circle_id and out.model_slug == w.model
    assert out.ttl_s == 600 and out.renew_after_s == 300
    assert out.lender.mesh_ip == w.lender.mesh_ip
    assert out.lender.port == 8347, "the port the engine reported, not an assumed default"
    assert len(out.token) == 43
    delta = out.expires_at - datetime.now(timezone.utc)
    assert timedelta(seconds=595) < delta <= timedelta(seconds=600)

    (to_lender,), (to_requester,) = w.lender.sidecar.of_type("circle_peer_add"), w.requester.sidecar.of_type("circle_peer_add")
    lender_msg = SignalCirclePeerAdd.model_validate(to_lender)
    requester_msg = SignalCirclePeerAdd.model_validate(to_requester)
    assert (lender_msg.role, requester_msg.role) == ("lender", "requester")
    assert str(lender_msg.link_id) == str(requester_msg.link_id) == str(out.link_id)
    # Each sidecar is told about the OTHER machine.
    assert lender_msg.peer_wg_public_key == w.requester.wg_key
    assert lender_msg.peer_mesh_ip == w.requester.mesh_ip
    assert requester_msg.peer_wg_public_key == w.lender.wg_key
    assert requester_msg.peer_mesh_ip == w.lender.mesh_ip
    assert lender_msg.port == requester_msg.port == 8347
    assert lender_msg.ttl_s == requester_msg.ttl_s == 600
    # Only the lender gets the hash, and it is the hash of the token returned.
    assert lender_msg.token_sha256 == hashlib.sha256(out.token.encode()).hexdigest()
    assert requester_msg.token_sha256 is None
    # Nothing else is sent: no extend, no remove.
    assert [m["type"] for m in w.lender.sidecar.sent] == ["circle_peer_add"]
    assert [m["type"] for m in w.requester.sidecar.sent] == ["circle_peer_add"]


@pytest.mark.asyncio
async def test_only_the_hash_of_the_token_is_stored(make_world) -> None:
    w = await make_world()
    out = await create(w)
    row = await w.row(out.link_id)
    assert row.token_hash == hashlib.sha256(out.token.encode()).hexdigest()
    assert out.token not in await raw_dump(w.factory, out.link_id)
    assert (row.requester_member_id, row.lender_member_id) == (w.member_id, w.lender.member_id)
    assert (row.requester_node_id, row.lender_node_id) == (w.requester.node_id, w.lender.node_id)
    assert (row.lender_peer_id, row.lender_port, row.model_slug) == (w.lender.peer_id, 8347, w.model)
    assert (row.ended_at, row.end_reason, row.renewals) == (None, None, 0)


@pytest.mark.asyncio
async def test_what_the_sidecars_and_the_requester_are_told_names_no_one(make_world) -> None:
    """No member id, node id, node name, hostname or circle name reaches a
    sidecar, and the requester learns nothing about the lender beyond where to
    connect."""
    w = await make_world()
    out = await create(w)
    sidecar_traffic = json.dumps(w.lender.sidecar.sent + w.requester.sidecar.sent)
    answer = out.model_dump_json()
    secrets_ = {
        "requester member": str(w.requester.member_id), "lender member": str(w.lender.member_id),
        "requester node": str(w.requester.node_id), "lender node": str(w.lender.node_id),
        "lender peer": str(w.lender.peer_id), "requester node name": w.requester.node_name,
        "lender node name": w.lender.node_name, "lender hostname": w.lender.hostname,
        "circle name": w.circle_name, "circle id": str(w.circle_id),
    }
    for what, value in secrets_.items():
        assert value not in sidecar_traffic, f"sidecar message leaks the {what}"
    for what in ("lender member", "lender node", "lender peer", "lender node name", "lender hostname"):
        assert secrets_[what] not in answer, f"the answer leaks the {what}"
    assert set(json.loads(answer)) == {
        "link_id", "circle_id", "model_slug", "token", "lender", "ttl_s", "renew_after_s",
        "expires_at", "ready",
    }


@pytest.mark.asyncio
async def test_a_repeat_create_for_the_pair_supersedes_the_old_link(make_world) -> None:
    w = await make_world()
    first = await create(w)
    second = await create(w)
    assert first.link_id != second.link_id and first.token != second.token

    old, new = await w.row(first.link_id), await w.row(second.link_id)
    assert (old.end_reason, new.ended_at) == ("superseded", None)
    assert [r.id for r in await w.live()] == [new.id], "one live link per pair"
    # Per sidecar: add(old), add(new), then remove(old, superseded), in that order.
    for car in (w.requester.sidecar, w.lender.sidecar):
        kinds = [(m["type"], m["link_id"]) for m in car.sent]
        assert kinds == [
            ("circle_peer_add", str(first.link_id)),
            ("circle_peer_add", str(second.link_id)),
            ("circle_peer_remove", str(first.link_id)),
        ]
        assert car.of_type("circle_peer_remove")[0]["reason"] == "superseded"


@pytest.mark.asyncio
async def test_a_lapsed_unswept_row_for_the_pair_does_not_block_a_new_link(make_world) -> None:
    w = await make_world()
    first = await create(w)
    async with w.factory() as db:
        await db.execute(text("UPDATE circle_links SET expires_at = now() - interval '1 minute', "
                              "created_at = now() - interval '11 minutes' WHERE id = :i"), {"i": first.link_id})
        await db.commit()
    second = await create(w)
    assert (await w.row(first.link_id)).end_reason == "expired"
    assert [r.id for r in await w.live()] == [second.link_id]


# ---------------------------------------------------------------------------
# Transport policy
# ---------------------------------------------------------------------------


async def _set_endpoints(w, requester_ep, lender_ep):
    async with w.factory() as db:
        await db.execute(text("UPDATE mesh_nodes SET last_endpoint = :e WHERE id = :i"),
                         {"e": requester_ep, "i": w.requester.node_id})
        await db.execute(text("UPDATE mesh_nodes SET last_endpoint = :e WHERE id = :i"),
                         {"e": lender_ep, "i": w.lender.node_id})
        await db.commit()


def _adds(w):
    (lender,), (requester,) = w.lender.sidecar.of_type("circle_peer_add"), w.requester.sidecar.of_type("circle_peer_add")
    return lender, requester


@pytest.mark.asyncio
async def test_relay_only_is_the_default_and_discloses_no_address(make_world) -> None:
    w = await make_world()
    await _set_endpoints(w, "198.51.100.4:51820", "203.0.113.9:51820")
    mesh._local_endpoints[w.lender.node_id] = ["192.168.1.23:51820"]
    mesh._local_endpoints[w.requester.node_id] = ["192.168.1.40:51820"]
    assert get_settings().circle_link_direct is False
    await create(w)
    for msg in _adds(w):
        assert msg["direct_window_ms"] == 0
        assert msg["peer_endpoint"] is None
        assert msg["peer_local_endpoints"] == []
    blob = json.dumps(w.lender.sidecar.sent + w.requester.sidecar.sent)
    for address in ("198.51.100.4", "203.0.113.9", "192.168.1"):
        assert address not in blob


@pytest.mark.asyncio
async def test_direct_policy_shares_each_machines_public_endpoint(make_world) -> None:
    w = await make_world()
    w_env = pytest.MonkeyPatch()
    try:
        w_env.setenv("POTLUCK_CIRCLE_LINK_DIRECT", "1")
        get_settings.cache_clear()
        await _set_endpoints(w, "198.51.100.4:51820", "203.0.113.9:51820")
        mesh._local_endpoints[w.lender.node_id] = ["192.168.1.23:51820"]
        await create(w)
        lender_msg, requester_msg = _adds(w)
        assert lender_msg["peer_endpoint"] == "198.51.100.4:51820"      # the requester's
        assert requester_msg["peer_endpoint"] == "203.0.113.9:51820"    # the lender's
        assert lender_msg["direct_window_ms"] == requester_msg["direct_window_ms"] == 3000
        # Different public addresses: no LAN candidate crosses households.
        assert lender_msg["peer_local_endpoints"] == requester_msg["peer_local_endpoints"] == []
    finally:
        w_env.undo()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_direct_policy_shares_lan_candidates_only_behind_one_public_address(make_world) -> None:
    w = await make_world()
    w_env = pytest.MonkeyPatch()
    try:
        w_env.setenv("POTLUCK_CIRCLE_LINK_DIRECT", "1")
        get_settings.cache_clear()
        # Same public address, different ports: one home or office.
        await _set_endpoints(w, "198.51.100.4:51820", "198.51.100.4:51999")
        mesh._local_endpoints[w.lender.node_id] = ["192.168.1.23:51999", "8.8.8.8:1", "100.64.9.9:1"]
        mesh._local_endpoints[w.requester.node_id] = ["192.168.1.40:51820"]
        await create(w)
        lender_msg, requester_msg = _adds(w)
        # Public candidates and anything not RFC1918 are filtered again here.
        assert requester_msg["peer_local_endpoints"] == ["192.168.1.23:51999"]
        assert lender_msg["peer_local_endpoints"] == ["192.168.1.40:51820"]
    finally:
        w_env.undo()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_direct_policy_with_unknown_endpoints_still_sends_a_valid_message(make_world) -> None:
    w = await make_world()
    w_env = pytest.MonkeyPatch()
    try:
        w_env.setenv("POTLUCK_CIRCLE_LINK_DIRECT", "1")
        get_settings.cache_clear()
        await create(w)  # neither node has reported an endpoint
        for msg in _adds(w):
            assert msg["peer_endpoint"] is None and msg["peer_local_endpoints"] == []
            assert msg["direct_window_ms"] == 3000
    finally:
        w_env.undo()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Validation order and the errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_gets_circle_not_found_and_so_does_a_missing_circle(make_world) -> None:
    w = await make_world()
    stranger = uuid.uuid4()
    for circle in (w.circle_id, uuid.uuid4()):
        exc = await refused(create(w, member=stranger, circle=circle))
        assert (exc.status_code, code_of(exc)) == (404, "circle_not_found")
    assert w.lender.sidecar.sent == [] and await w.rows() == []


@pytest.mark.asyncio
async def test_membership_is_checked_before_the_machine(make_world) -> None:
    """A non-member naming someone else's machine learns nothing about it."""
    w = await make_world()
    exc = await refused(create(w, member=uuid.uuid4(), node=w.lender.node_id))
    assert code_of(exc) == "circle_not_found"


@pytest.mark.asyncio
async def test_a_machine_that_is_not_yours_and_one_that_does_not_exist_look_the_same(make_world) -> None:
    w = await make_world()
    for node in (w.lender.node_id, uuid.uuid4()):
        exc = await refused(create(w, node=node))
        assert (exc.status_code, code_of(exc)) == (404, "node_not_found")


@pytest.mark.asyncio
async def test_a_circle_member_cannot_make_a_link_from_another_members_machine(make_world) -> None:
    w = await make_world()
    # The lender's member is in the circle too and names the requester's machine.
    exc = await refused(create(w, member=w.lender.member_id, node=w.requester.node_id))
    assert code_of(exc) == "node_not_found"


@pytest.mark.asyncio
async def test_requester_sidecar_must_be_connected_and_advertise_links(make_world) -> None:
    offline = await make_world(requester_connected=False)
    exc = await refused(create(offline))
    assert (exc.status_code, code_of(exc)) == (409, "requester_sidecar_offline")

    old = await make_world(requester_feature=False)
    exc = await refused(create(old))
    assert (exc.status_code, code_of(exc)) == (409, "requester_sidecar_unsupported")
    assert old.lender.sidecar.sent == [] and await old.rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lender_kwargs", [
    {"feature": False},                 # old sidecar: no circle_link_v1 in hello
    {"connected": False},               # sidecar not on /mesh/signal
    {"bound": False},                   # engine not bound to a mesh node (old engine, or refused)
    {"serves_circle": False},           # sharing with the open pool only
    {"in_circle": False},               # not in this circle at all
    {"model": "some-other-model"},      # does not offer the model
])
async def test_no_lender_when_nobody_link_capable_offers_the_model(make_world, lender_kwargs) -> None:
    w = await make_world(lender_kwargs=lender_kwargs)
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (409, "no_lender")
    assert await w.rows() == [] and w.requester.sidecar.sent == []


@pytest.mark.asyncio
async def test_a_circle_request_is_never_answered_from_the_open_pool(make_world) -> None:
    """A machine outside the circle that offers the model to the whole pool is
    not a lender, however capable and however well it scores."""
    w = await make_world(lender_kwargs={"in_circle": False, "serves_pool": True, "tokens_per_second": 500.0})
    exc = await refused(create(w))
    assert code_of(exc) == "no_lender"
    # It would be chosen for a pool request: the filter, not its absence, kept it out.
    async with w.factory() as db:
        peer, scope = await select_peer_for_request(
            db, model_slug=w.model, requester_member_id=w.member_id, scope="pool")
    assert peer is not None and scope == "pool"


@pytest.mark.asyncio
async def test_the_requesters_own_machines_are_never_the_lender(make_world) -> None:
    w = await make_world(lenders=0)
    tag = uuid.uuid4().hex[:8]
    async with w.factory() as db:
        async with db.begin():
            peer = Peer(member_id=w.member_id, public_key=f"own-{tag}", hardware_summary={}, status="active")
            db.add(peer)
            await db.flush()
            db.add(PeerCapability(peer_id=peer.id, model_slug=w.model, quantization="q4"))
            db.add(MemberKey(public_key=f"own-{tag}", member_id=w.member_id, added_via="seed"))
    await register_peer(PeerConnection(
        peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(), member_id=w.member_id,
        mesh_node_id=w.requester.node_id, circle_port=8340))
    try:
        exc = await refused(create(w))
        assert code_of(exc) == "no_lender"
    finally:
        await unregister_peer(peer.id)
        async with w.factory() as db:
            await db.execute(text("DELETE FROM peers WHERE id = :i"), {"i": peer.id})
            await db.execute(text("DELETE FROM member_keys WHERE public_key = :k"), {"k": f"own-{tag}"})
            await db.commit()


@pytest.mark.asyncio
async def test_lender_choice_is_the_relays_choice_among_link_capable_machines(make_world) -> None:
    w = await make_world(lenders=0)
    slow = await w.add_lender(tokens_per_second=10.0)
    fast = await w.add_lender(tokens_per_second=90.0)
    async with w.factory() as db:
        relay_peer, _ = await select_peer_for_request(
            db, model_slug=w.model, requester_member_id=w.member_id, scope="circle",
            circle_member_ids=await lender_member_ids(db, w.circle_id, w.member_id))
    assert relay_peer.peer_id == fast.peer_id

    out = await create(w)
    assert (await w.row(out.link_id)).lender_peer_id == fast.peer_id
    assert out.lender.mesh_ip == fast.mesh_ip

    # Take the better machine's sidecar off the mesh: the link goes to the other,
    # while the relay would still pick the better one.
    mesh._signal_features[fast.node_id] = frozenset()
    out2 = await create(w)
    assert (await w.row(out2.link_id)).lender_peer_id == slow.peer_id
    async with w.factory() as db:
        again, _ = await select_peer_for_request(
            db, model_slug=w.model, requester_member_id=w.member_id, scope="circle",
            circle_member_ids=await lender_member_ids(db, w.circle_id, w.member_id))
    assert again.peer_id == fast.peer_id


@pytest.mark.asyncio
async def test_exclude_link_ids_skips_the_lender_behind_a_failed_link(make_world) -> None:
    w = await make_world(lenders=0)
    better = await w.add_lender(tokens_per_second=90.0)
    other = await w.add_lender(tokens_per_second=10.0)
    first = await create(w)
    assert first.lender.mesh_ip == better.mesh_ip
    second = await create(w, exclude=[first.link_id])
    assert second.lender.mesh_ip == other.mesh_ip
    # With both excluded there is nobody left.
    exc = await refused(create(w, exclude=[first.link_id, second.link_id]))
    assert code_of(exc) == "no_lender"


@pytest.mark.asyncio
async def test_exclude_link_ids_ignores_links_that_are_not_the_callers(make_world) -> None:
    """Naming another member's link must not exclude the lender behind it: that
    would let one member shut a lender out of another's requests, and tell them
    which lender the other member is using."""
    w = await make_world(lenders=0)
    only = await w.add_lender()
    other = await w.add_requester()
    theirs = await create(w, member=other.member_id, node=other.node_id)
    assert theirs.lender.mesh_ip == only.mesh_ip
    out = await create(w, exclude=[theirs.link_id, uuid.uuid4()])
    assert out.lender.mesh_ip == only.mesh_ip


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_lender_ack_is_a_504_and_revokes_the_link(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": "silent"})
    started = time.monotonic()
    exc = await refused(create(w))
    assert 0.9 < time.monotonic() - started < 3.0
    assert (exc.status_code, code_of(exc)) == (504, "sidecar_timeout")
    (row,) = await w.rows()
    assert (row.end_reason, row.ended_at is not None) == ("setup_failed", True)
    for car in (w.requester.sidecar, w.lender.sidecar):
        removes = car.of_type("circle_peer_remove")
        assert len(removes) == 1 and removes[0]["reason"] == "revoked"
        assert removes[0]["link_id"] == str(row.id)


@pytest.mark.asyncio
async def test_a_missing_requester_ack_is_a_504_too(make_world) -> None:
    w = await make_world(requester_sidecar="silent")
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (504, "sidecar_timeout")
    assert (await w.rows())[0].end_reason == "setup_failed"


@pytest.mark.asyncio
async def test_a_rejecting_lender_is_a_502_with_the_reason_and_revokes(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": ("reject", "address_conflict")})
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (502, "sidecar_rejected")
    assert exc.detail["reason"] == "address_conflict"
    assert exc.detail["message"]
    (row,) = await w.rows()
    assert row.end_reason == "setup_failed"
    assert w.requester.sidecar.of_type("circle_peer_remove")[0]["reason"] == "revoked"


@pytest.mark.asyncio
async def test_a_rejecting_lender_gets_one_retry_with_another(make_world) -> None:
    w = await make_world(lenders=0)
    bad = await w.add_lender(tokens_per_second=90.0, sidecar=("reject", "limit"))
    good = await w.add_lender(tokens_per_second=10.0)
    out = await create(w)
    assert out.lender.mesh_ip == good.mesh_ip
    rows = await w.rows()
    assert {r.lender_node_id: r.end_reason for r in rows} == {bad.node_id: "setup_failed", good.node_id: None}
    # The refusing lender got its add and then the revocation; the good one only its add.
    assert [m["type"] for m in bad.sidecar.sent] == ["circle_peer_add", "circle_peer_remove"]
    assert [m["type"] for m in good.sidecar.sent] == ["circle_peer_add"]


@pytest.mark.asyncio
async def test_the_retry_is_one_retry_not_a_search(make_world) -> None:
    w = await make_world(lenders=0)
    for tps in (90.0, 50.0, 10.0):
        await w.add_lender(tokens_per_second=tps, sidecar=("reject", "unsupported"))
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (502, "sidecar_rejected")
    assert len(await w.rows()) == 2, "the first lender and one retry"


@pytest.mark.asyncio
async def test_a_rejecting_requester_is_a_502_and_is_not_retried(make_world) -> None:
    w = await make_world(lenders=0, requester_sidecar=("reject", "limit"))
    await w.add_lender(tokens_per_second=90.0)
    await w.add_lender(tokens_per_second=10.0)
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (502, "sidecar_rejected")
    assert exc.detail["reason"] == "limit"
    assert len(await w.rows()) == 1


@pytest.mark.asyncio
async def test_a_reason_the_coordinator_does_not_know_is_not_echoed(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": ("reject", "<script>alert(1)</script>")})
    exc = await refused(create(w))
    assert exc.detail["reason"] == "unknown"


@pytest.mark.asyncio
async def test_a_lender_sidecar_that_vanishes_during_setup_is_retried_like_a_refusal(make_world) -> None:
    w = await make_world(lenders=0)
    gone = await w.add_lender(tokens_per_second=90.0)
    good = await w.add_lender(tokens_per_second=10.0)
    # The sidecar drops off between lender choice and setup.
    gone.sidecar.on_send = lambda payload: None
    mesh._signal_connections.pop(gone.node_id)
    mesh._signal_connections[gone.node_id] = None  # type: ignore[assignment]  # not sendable
    try:
        out = await create(w)
    finally:
        mesh._signal_connections.pop(gone.node_id, None)
    assert out.lender.mesh_ip == good.mesh_ip


@pytest.mark.asyncio
async def test_an_ack_only_counts_from_the_node_it_was_sent_to(make_world) -> None:
    """A circle member who learned a link id could try to confirm the other end.
    The ack is matched on the connection it arrived on, so it finds nothing."""
    w = await make_world(lender_kwargs={"sidecar": "silent"})
    spoof_from = w.requester.node_id  # a legitimate node, but not the lender

    def forge(payload: dict) -> None:
        import asyncio
        asyncio.get_running_loop().create_task(circle_links.handle_ack(
            spoof_from, {"type": "circle_peer_ack", "link_id": payload["link_id"], "op": "add",
                         "state": "ready", "reason": None}))

    w.lender.sidecar.on_send = lambda p: forge(p) if p["type"] == "circle_peer_add" else None
    exc = await refused(create(w))
    assert code_of(exc) == "sidecar_timeout"


@pytest.mark.asyncio
async def test_an_ack_for_the_wrong_op_or_link_does_not_confirm_setup(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": "silent"})

    def wrong(payload: dict) -> None:
        import asyncio
        for link, op in ((payload["link_id"], "extend"), (str(uuid.uuid4()), "add")):
            asyncio.get_running_loop().create_task(circle_links.handle_ack(
                w.lender.node_id,
                {"type": "circle_peer_ack", "link_id": link, "op": op, "state": "ready", "reason": None}))

    w.lender.sidecar.on_send = lambda p: wrong(p) if p["type"] == "circle_peer_add" else None
    exc = await refused(create(w))
    assert code_of(exc) == "sidecar_timeout"


@pytest.mark.asyncio
async def test_the_ack_handler_swallows_whatever_goes_wrong_inside_it(monkeypatch) -> None:
    from coordinator.schemas import mesh as mesh_schemas

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mesh_schemas.SignalCirclePeerAck, "model_validate", boom)
    await circle_links.handle_ack(uuid.uuid4(), {"type": "circle_peer_ack"})


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [
    None, "ack", 7, [], {}, {"type": "circle_peer_ack"},
    {"type": "circle_peer_ack", "link_id": "nope", "op": "add", "state": "ready"},
    {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "sideways", "state": "ready"},
    {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "add", "state": "maybe"},
    {"type": "circle_peer_ack", "link_id": str(uuid.uuid4()), "op": "add", "state": "rejected",
     "reason": "x" * 5000},
])
async def test_the_ack_handler_cannot_be_made_to_raise(junk) -> None:
    await circle_links.handle_ack(uuid.uuid4(), junk)  # returns; nothing to assert but no raise


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def test_the_limits_are_the_decided_ones() -> None:
    assert circle_links.MAX_LIVE_LINKS_PER_MEMBER == 8
    assert circle_links.MAX_CREATES_PER_MINUTE == 30
    assert circle_links.ACK_TIMEOUT_S == 2.0
    assert circle_links.LENDER_DISCONNECT_GRACE_S == 15.0
    assert circle_links.RETENTION == timedelta(hours=24)


@pytest.mark.asyncio
async def test_live_link_limit_per_member(make_world, links_on) -> None:
    w = await make_world(lenders=0)
    first = await w.add_lender(tokens_per_second=90.0)
    await w.add_lender(tokens_per_second=10.0)
    links_on.setattr(circle_links, "MAX_LIVE_LINKS_PER_MEMBER", 1)

    one = await create(w)
    assert one.lender.mesh_ip == first.mesh_ip
    # A repeat create for the same pair replaces its row, so it is not a second link.
    again = await create(w)
    assert again.lender.mesh_ip == first.mesh_ip
    # A link to a different machine would be a second live one.
    exc = await refused(create(w, exclude=[again.link_id]))
    assert (exc.status_code, code_of(exc)) == (429, "too_many_links")
    assert exc.headers is None or "Retry-After" not in exc.headers
    assert [r.id for r in await w.live()] == [again.link_id]
    # Ending the first makes room.
    await end(w, again.link_id)
    assert (await create(w, exclude=[again.link_id])).lender.mesh_ip != first.mesh_ip


@pytest.mark.asyncio
async def test_expired_links_do_not_count_against_the_live_limit(make_world, links_on) -> None:
    w = await make_world(lenders=0)
    await w.add_lender(tokens_per_second=90.0)
    other = await w.add_lender(tokens_per_second=10.0)
    links_on.setattr(circle_links, "MAX_LIVE_LINKS_PER_MEMBER", 1)
    first = await create(w)
    async with w.factory() as db:
        await db.execute(text("UPDATE circle_links SET expires_at = now() - interval '1 second', "
                              "created_at = now() - interval '11 minutes' WHERE id = :i"), {"i": first.link_id})
        await db.commit()
    out = await create(w, exclude=[first.link_id])
    assert out.lender.mesh_ip == other.mesh_ip


@pytest.mark.asyncio
async def test_create_rate_limit_per_member(make_world, links_on) -> None:
    w = await make_world()
    links_on.setattr(circle_links, "MAX_CREATES_PER_MINUTE", 3)
    for _ in range(3):
        await create(w)
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc)) == (429, "too_many_links")
    assert 1 <= int(exc.headers["Retry-After"]) <= 60
    assert len(await w.rows()) == 3, "the refused attempt made nothing"


@pytest.mark.asyncio
async def test_refused_requests_do_not_spend_the_members_create_budget(make_world, links_on) -> None:
    w = await make_world()
    links_on.setattr(circle_links, "MAX_CREATES_PER_MINUTE", 2)
    for _ in range(10):  # a stranger probing, and a wrong machine
        await refused(create(w, member=uuid.uuid4()))
        await refused(create(w, node=uuid.uuid4()))
    await create(w)
    await create(w)  # both of the member's own creates still fit


# ---------------------------------------------------------------------------
# Renew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renew_extends_the_link_and_sends_extend_to_both(make_world, links_on) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await db.execute(text("UPDATE circle_links SET expires_at = now() + interval '60 seconds', "
                              "created_at = now() - interval '9 minutes' WHERE id = :i"), {"i": out.link_id})
        await db.commit()

    got = await renew(w, out.link_id)
    assert got.link_id == out.link_id and got.ttl_s == 600 and got.renew_after_s == 300
    assert got.delivered.requester is True and got.delivered.lender is True
    assert timedelta(seconds=595) < got.expires_at - datetime.now(timezone.utc) <= timedelta(seconds=600)

    row = await w.row(out.link_id)
    assert (row.renewals, row.expires_at, row.renewed_at is not None) == (1, got.expires_at, True)
    for car in (w.requester.sidecar, w.lender.sidecar):
        (extend,) = car.of_type("circle_peer_extend")
        SignalCirclePeerExtend.model_validate(extend)
        assert extend["link_id"] == str(out.link_id) and extend["ttl_s"] == 600
        assert extend["peer_endpoint"] is None, "relay-only discloses nothing on renewal either"


@pytest.mark.asyncio
async def test_renew_under_direct_policy_sends_a_fresh_endpoint(make_world, links_on) -> None:
    w = await make_world()
    out = await create(w)
    links_on.setenv("POTLUCK_CIRCLE_LINK_DIRECT", "1")
    get_settings.cache_clear()
    await _set_endpoints(w, "198.51.100.77:40000", "203.0.113.88:41000")
    await settle(w, out.link_id)
    await renew(w, out.link_id)
    assert w.lender.sidecar.of_type("circle_peer_extend")[0]["peer_endpoint"] == "198.51.100.77:40000"
    assert w.requester.sidecar.of_type("circle_peer_extend")[0]["peer_endpoint"] == "203.0.113.88:41000"


@pytest.mark.asyncio
async def test_renew_resends_the_add_to_a_sidecar_that_lost_the_link(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": {"extend": ("reject", "unknown_grant")}})
    out = await create(w)
    await settle(w, out.link_id)
    got = await renew(w, out.link_id)
    assert got.delivered.requester is True and got.delivered.lender is True
    adds = w.lender.sidecar.of_type("circle_peer_add")
    assert len(adds) == 2 and adds[0]["link_id"] == adds[1]["link_id"] == str(out.link_id)
    assert adds[1]["token_sha256"] == hashlib.sha256(out.token.encode()).hexdigest()
    assert len(w.requester.sidecar.of_type("circle_peer_add")) == 1, "only the one that asked"
    assert 595 <= adds[1]["ttl_s"] <= 600


@pytest.mark.asyncio
async def test_renew_with_a_silent_sidecar_still_succeeds_and_says_who_missed_it(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": {"add": "ready", "extend": "silent"}})
    out = await create(w)
    await settle(w, out.link_id)
    got = await renew(w, out.link_id)
    assert (got.delivered.requester, got.delivered.lender) == (True, False)
    assert (await w.row(out.link_id)).renewals == 1


@pytest.mark.asyncio
async def test_renew_with_a_sidecar_off_the_mesh_says_so(make_world) -> None:
    w = await make_world()
    out = await create(w)
    mesh._signal_connections.pop(w.requester.node_id)
    await settle(w, out.link_id)
    got = await renew(w, out.link_id)
    assert (got.delivered.requester, got.delivered.lender) == (False, True)


@pytest.mark.asyncio
async def test_renew_is_404_unless_the_link_is_yours(make_world) -> None:
    w = await make_world()
    out = await create(w)
    for member, circle, link in (
        (w.lender.member_id, w.circle_id, out.link_id),   # the other end of the link
        (uuid.uuid4(), w.circle_id, out.link_id),         # a stranger
        (w.member_id, uuid.uuid4(), out.link_id),         # your link, the wrong circle
        (w.member_id, w.circle_id, uuid.uuid4()),         # no such link
    ):
        exc = await refused(renew(w, link, member=member, circle=circle))
        assert (exc.status_code, code_of(exc)) == (404, "link_not_found")
    assert (await w.row(out.link_id)).renewals == 0


@pytest.mark.asyncio
async def test_renew_of_an_ended_link_is_410_with_the_reason_and_changes_nothing(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await end(w, out.link_id)
    sent = len(w.lender.sidecar.sent)
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, code_of(exc), exc.detail["reason"]) == (410, "link_ended", "ended")
    assert len(w.lender.sidecar.sent) == sent
    assert (await w.row(out.link_id)).renewals == 0


@pytest.mark.asyncio
async def test_renew_of_a_lapsed_link_is_410_expired_and_ends_it(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await db.execute(text("UPDATE circle_links SET expires_at = now() - interval '1 second', "
                              "created_at = now() - interval '11 minutes' WHERE id = :i"), {"i": out.link_id})
        await db.commit()
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "expired")
    assert (await w.row(out.link_id)).end_reason == "expired"
    assert w.lender.sidecar.of_type("circle_peer_remove")[0]["reason"] == "expired"


@pytest.mark.asyncio
async def test_renew_rechecks_that_the_requester_is_still_in_the_circle(make_world) -> None:
    w = await make_world(requester_owns_circle=False)
    out = await create(w)
    async with w.factory() as db:  # changed behind the routes' backs
        await db.execute(text("DELETE FROM circle_memberships WHERE circle_id = :c AND member_id = :m"),
                         {"c": w.circle_id, "m": w.member_id})
        await db.commit()
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "member_left")
    assert (await w.row(out.link_id)).end_reason == "member_left"
    assert w.lender.sidecar.of_type("circle_peer_remove")


@pytest.mark.asyncio
async def test_renew_rechecks_that_the_lender_may_still_lend(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await db.execute(text("DELETE FROM circle_memberships WHERE circle_id = :c AND member_id = :m"),
                         {"c": w.circle_id, "m": w.lender.member_id})
        await db.commit()
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "member_removed")


@pytest.mark.asyncio
async def test_renew_rechecks_that_the_lender_still_shares_with_circles(make_world) -> None:
    from coordinator.services import peer_registry

    w = await make_world()
    out = await create(w)
    peer_registry._peers[w.lender.peer_id].serves_circle = False
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "sharing_off")
    assert (await w.row(out.link_id)).end_reason == "sharing_off"


@pytest.mark.asyncio
async def test_renew_rechecks_that_the_lender_engine_is_connected(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await unregister_peer(w.lender.peer_id)
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "lender_gone")
    assert (await w.row(out.link_id)).end_reason == "lender_gone"


@pytest.mark.asyncio
async def test_renew_during_the_disconnect_grace_keeps_the_link(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await unregister_peer(w.lender.peer_id)
    import asyncio
    circle_links._grace_tasks[w.lender.peer_id] = asyncio.create_task(asyncio.sleep(30))
    await settle(w, out.link_id)
    got = await renew(w, out.link_id)
    assert got.link_id == out.link_id and (await w.row(out.link_id)).end_reason is None


# ---------------------------------------------------------------------------
# End
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_removes_the_link_and_tells_both_sidecars(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await end(w, out.link_id)
    row = await w.row(out.link_id)
    assert (row.end_reason, row.ended_at is not None) == ("ended", True)
    for car in (w.requester.sidecar, w.lender.sidecar):
        (remove,) = car.of_type("circle_peer_remove")
        SignalCirclePeerRemove.model_validate(remove)
        assert (remove["link_id"], remove["reason"]) == (str(out.link_id), "ended")


@pytest.mark.asyncio
async def test_end_is_idempotent_and_quiet_about_what_is_not_yours(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await end(w, out.link_id, member=uuid.uuid4())              # a stranger
    await end(w, out.link_id, member=w.lender.member_id)        # the other end
    await end(w, out.link_id, circle=uuid.uuid4())              # wrong circle
    await end(w, uuid.uuid4())                                  # no such link
    assert (await w.row(out.link_id)).ended_at is None
    assert w.lender.sidecar.of_type("circle_peer_remove") == []

    await end(w, out.link_id)
    await end(w, out.link_id)
    assert len(w.lender.sidecar.of_type("circle_peer_remove")) == 1, "the second end sent nothing"


@pytest.mark.asyncio
async def test_a_removal_that_cannot_be_delivered_does_not_fail_the_end(make_world) -> None:
    w = await make_world()
    out = await create(w)
    mesh._signal_connections.pop(w.lender.node_id)
    await end(w, out.link_id)
    assert (await w.row(out.link_id)).end_reason == "ended"
    assert w.requester.sidecar.of_type("circle_peer_remove")


# ---------------------------------------------------------------------------
# Races between setup and revocation
# ---------------------------------------------------------------------------


def _revoke_when_the_add_arrives(w, car, ack_node, *, op="add", reason="member_left"):
    """When `car` is sent an add, end the link (as a circle leave would) and
    only then acknowledge it, so the ack lands after the revocation."""
    import asyncio

    async def revoke_then_ack(payload):
        async with w.factory() as db:
            ended = await circle_links.end_for_circle_member(db, w.circle_id, w.lender.member_id, reason)
            await db.commit()
        await circle_links.push_removed(ended)
        await circle_links.handle_ack(ack_node, {
            "type": "circle_peer_ack", "link_id": payload["link_id"], "op": op, "state": "ready", "reason": None})

    def hook(payload):
        if payload["type"] == "circle_peer_add":
            asyncio.get_running_loop().create_task(revoke_then_ack(payload))

    car.on_send = hook


@pytest.mark.asyncio
async def test_a_link_revoked_while_its_adds_are_in_flight_is_not_handed_out(make_world) -> None:
    """A member leaving between a create's insert and its adds sends the removes
    first; the adds then reach the sidecars after them. The create looks again
    before answering, takes the link back, and says it has ended."""
    w = await make_world(lender_kwargs={"sidecar": "silent"})
    _revoke_when_the_add_arrives(w, w.lender.sidecar, w.lender.node_id)
    exc = await refused(create(w))
    assert (exc.status_code, code_of(exc), exc.detail["reason"]) == (410, "link_ended", "member_left")
    for car in (w.requester.sidecar, w.lender.sidecar):
        kinds = [m["type"] for m in car.sent]
        assert kinds[0] == "circle_peer_add" and kinds[-1] == "circle_peer_remove", kinds
    (row,) = await w.rows()
    assert row.end_reason == "member_left"


@pytest.mark.asyncio
async def test_a_renew_never_brings_back_a_link_that_was_revoked_meanwhile(make_world) -> None:
    w = await make_world()
    out = await create(w)
    for car in (w.requester.sidecar, w.lender.sidecar):
        car.sent.clear()
    # The lender lost the link, so it refuses the extension and the coordinator sends
    # the add again; as that goes out, the circle ends the link, and only then does
    # the lender acknowledge.
    await settle(w, out.link_id)
    w.lender.sidecar.behaviour = {"extend": ("reject", "unknown_grant"), "add": "silent"}
    _revoke_when_the_add_arrives(w, w.lender.sidecar, w.lender.node_id)
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, code_of(exc)) == (410, "link_ended")
    assert [m["type"] for m in w.lender.sidecar.sent][-1] == "circle_peer_remove"


@pytest.mark.asyncio
async def test_revoked_meanwhile_reports_and_undoes(make_world) -> None:
    w = await make_world()
    out = await create(w)
    row = await w.row(out.link_id)
    for car in (w.requester.sidecar, w.lender.sidecar):
        car.sent.clear()
    async with w.factory() as db:
        assert await circle_links._revoked_meanwhile(db, row, row.requester_node_id, row.lender_node_id) is None
        assert w.lender.sidecar.sent == []
        await end(w, out.link_id)
        w.lender.sidecar.sent.clear()
        w.requester.sidecar.sent.clear()
        assert await circle_links._revoked_meanwhile(db, row, row.requester_node_id, row.lender_node_id) == "ended"
        assert w.lender.sidecar.of_type("circle_peer_remove") and w.requester.sidecar.of_type("circle_peer_remove")
        # A row that is gone altogether (its circle or machine was deleted) counts as over too.
        gone = uuid.uuid4()
        w.lender.sidecar.sent.clear()
        assert await circle_links._revoked_meanwhile(db, circle_links.EndedLink(gone, row.requester_node_id, row.lender_node_id, "x"),
                                                     row.requester_node_id, row.lender_node_id) == "circle_deleted"
        assert w.lender.sidecar.of_type("circle_peer_remove")


@pytest.mark.asyncio
async def test_a_create_cancelled_mid_setup_leaves_no_live_link(make_world) -> None:
    import asyncio

    w = await make_world(lender_kwargs={"sidecar": "silent"})
    task = asyncio.create_task(create(w))
    for _ in range(100):  # until the adds are out
        await asyncio.sleep(0.01)
        if w.lender.sidecar.sent:
            break
    assert w.lender.sidecar.of_type("circle_peer_add")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        await asyncio.sleep(0.02)
        if (await w.rows()) and (await w.rows())[0].ended_at is not None:
            break
    (row,) = await w.rows()
    assert row.end_reason == "setup_failed"
    assert w.lender.sidecar.of_type("circle_peer_remove"), "the sidecars are told"


# ---------------------------------------------------------------------------
# A renewal messages another household's sidecar, so its rate is bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_renewals_are_spaced_out_so_a_member_cannot_flood_a_lenders_sidecar(make_world) -> None:
    w = await make_world()
    out = await create(w)
    sent_before = len(w.lender.sidecar.sent)

    for _ in range(5):  # as fast as the member likes, right after creating
        exc = await refused(renew(w, out.link_id))
        assert (exc.status_code, code_of(exc)) == (429, "too_many_links")
        assert 1 <= int(exc.headers["Retry-After"]) <= 60 + 1
    assert len(w.lender.sidecar.sent) == sent_before, "nothing reached the lender's sidecar"
    assert (await w.row(out.link_id)).renewals == 0

    await settle(w, out.link_id)         # half a lifetime later, as an honest engine would
    assert (await renew(w, out.link_id)).delivered.lender is True
    exc = await refused(renew(w, out.link_id))   # and again at once
    assert code_of(exc) == "too_many_links"
    assert len(w.lender.sidecar.of_type("circle_peer_extend")) == 1


@pytest.mark.asyncio
async def test_a_sidecar_that_stops_reading_cannot_hold_a_create_open(make_world) -> None:
    w = await make_world(lender_kwargs={"sidecar": "silent"})
    w.lender.sidecar.hang = True          # the send itself never completes
    started = time.monotonic()
    exc = await refused(create(w))
    assert time.monotonic() - started < 2.5, "bounded by the ack wait, not by the socket"
    assert (exc.status_code, code_of(exc)) == (502, "sidecar_rejected")
    assert (await w.rows())[0].end_reason == "setup_failed"
