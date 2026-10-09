"""Direct circle links end when they should, and not otherwise.

Every way a link can lose its right to exist: the requester or the lender
leaves the circle or is removed from it, the circle is deleted, a legacy key is
taken out, a machine is removed, the lender stops sharing with circles, the
lender's engine disconnects (after a grace), and the plain passing of time. In
each, both sidecars are told to remove the link and the row records why. Then
the sweeper, the grace timers and the replay on a sidecar reconnect.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from coordinator.models import CircleLink, CircleMembership
from coordinator.routers import circles as circles_router
from coordinator.routers import mesh as mesh_router
from coordinator.services import circle_links
from coordinator.services.peer_registry import register_peer, unregister_peer, PeerConnection
from tests.circle_link_helpers import _WS
from tests.test_circle_links import (  # noqa: F401  (fixtures)
    code_of,
    create,
    end,
    links_on,
    make_world,
    refused,
    renew,
)


async def _removed_on_both(w, link_id, reason: str) -> None:
    for car in (w.requester.sidecar, w.lender.sidecar):
        removes = [m for m in car.of_type("circle_peer_remove") if m["link_id"] == str(link_id)]
        assert len(removes) == 1, f"exactly one remove for {link_id}"
        assert removes[0]["reason"] == reason


async def _membership_id(w, member_id) -> uuid.UUID:
    async with w.factory() as db:
        return (await db.execute(
            select(CircleMembership.id).where(
                CircleMembership.circle_id == w.circle_id, CircleMembership.member_id == member_id)
        )).scalar_one()


# ---------------------------------------------------------------------------
# The circle's own routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_lender_leaving_the_circle_ends_the_link_at_once(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await circles_router.leave_circle(w.circle_id, db=db, member_id=w.lender.member_id)
    row = await w.row(out.link_id)
    assert row.end_reason == "member_left"
    await _removed_on_both(w, out.link_id, "revoked")
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, code_of(exc), exc.detail["reason"]) == (410, "link_ended", "member_left")


@pytest.mark.asyncio
async def test_the_requester_leaving_the_circle_ends_the_link_at_once(make_world) -> None:
    w = await make_world(requester_owns_circle=False)
    out = await create(w)
    async with w.factory() as db:
        await circles_router.leave_circle(w.circle_id, db=db, member_id=w.member_id)
    assert (await w.row(out.link_id)).end_reason == "member_left"
    await _removed_on_both(w, out.link_id, "revoked")
    # They are no longer in the circle: a new link is refused, and the old one is gone.
    exc = await refused(create(w))
    assert code_of(exc) == "circle_not_found"


@pytest.mark.asyncio
async def test_removing_the_lender_from_the_circle_ends_the_link_and_renew_is_410(make_world) -> None:
    w = await make_world()
    out = await create(w)
    person = await _membership_id(w, w.lender.member_id)
    async with w.factory() as db:
        await circles_router.remove_person(w.circle_id, person, db=db, member_id=w.owner_id)
    assert (await w.row(out.link_id)).end_reason == "member_removed"
    await _removed_on_both(w, out.link_id, "revoked")
    exc = await refused(renew(w, out.link_id))
    assert (exc.status_code, exc.detail["reason"]) == (410, "member_removed")


@pytest.mark.asyncio
async def test_removing_the_requester_from_the_circle_ends_the_link(make_world) -> None:
    w = await make_world(requester_owns_circle=False)
    out = await create(w)
    person = await _membership_id(w, w.member_id)
    async with w.factory() as db:
        await circles_router.remove_person(w.circle_id, person, db=db, member_id=w.owner_id)
    assert (await w.row(out.link_id)).end_reason == "member_removed"
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_removing_someone_else_leaves_the_link_alone(make_world) -> None:
    w = await make_world()
    bystander = await w.add_member()
    out = await create(w)
    person = await _membership_id(w, bystander)
    async with w.factory() as db:
        await circles_router.remove_person(w.circle_id, person, db=db, member_id=w.owner_id)
    assert (await w.row(out.link_id)).ended_at is None
    assert w.lender.sidecar.of_type("circle_peer_remove") == []


@pytest.mark.asyncio
async def test_deleting_the_circle_ends_its_links_and_removes_the_rows(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await circles_router.delete_circle(w.circle_id, db=db, member_id=w.owner_id)
    # The sidecars were told before the rows went with the circle.
    await _removed_on_both(w, out.link_id, "revoked")
    assert await w.row(out.link_id) is None


@pytest.mark.asyncio
async def test_taking_out_a_legacy_key_ends_links_through_it(make_world) -> None:
    w = await make_world(lender_kwargs={"via": "legacy_key"})
    out = await create(w)  # a pasted key still lends, as on the relay
    assert out.lender.mesh_ip == w.lender.mesh_ip
    async with w.factory() as db:
        await circles_router.remove_circle_member(
            w.circle_id, w.lender.public_key, db=db, member_id=w.owner_id)
    assert (await w.row(out.link_id)).end_reason == "member_removed"
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_taking_out_a_key_does_not_end_a_link_the_circle_still_allows(make_world) -> None:
    w = await make_world()  # the lender holds a membership
    # Add the same member's key as a legacy entry too, then take it out again.
    from coordinator.models import CircleMember
    async with w.factory() as db:
        db.add(CircleMember(circle_id=w.circle_id, member_pubkey=w.lender.public_key))
        await db.commit()
    out = await create(w)
    async with w.factory() as db:
        await circles_router.remove_circle_member(
            w.circle_id, w.lender.public_key, db=db, member_id=w.owner_id)
    assert (await w.row(out.link_id)).ended_at is None


@pytest.mark.asyncio
async def test_a_failing_revocation_never_fails_the_circle_route(make_world, links_on) -> None:
    w = await make_world()
    out = await create(w)

    async def boom(*a, **k):
        raise RuntimeError("links table unavailable")

    links_on.setattr(circle_links, "end_for_circle_member", boom)
    async with w.factory() as db:
        await circles_router.leave_circle(w.circle_id, db=db, member_id=w.lender.member_id)
    async with w.factory() as db:  # the member did leave
        assert (await db.execute(select(CircleMembership).where(
            CircleMembership.member_id == w.lender.member_id,
            CircleMembership.circle_id == w.circle_id))).first() is None
    # And the sweeper's reconciliation catches the link the hook missed.
    swept = await circle_links.sweep_once(w.factory)
    assert swept.reconciled == 1
    assert (await w.row(out.link_id)).end_reason == "member_removed"
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_a_guarded_revocation_does_not_poison_the_routes_transaction(make_world) -> None:
    """A SQL error inside the hook (a missing table, say) is rolled back to a
    savepoint, so the route's own statements still commit."""
    w = await make_world()

    async def bad_sql(db, *_):
        await db.execute(text("SELECT * FROM table_that_does_not_exist"))
        return []

    async with w.factory() as db:
        assert await circle_links.guarded_end(db, bad_sql) == []
        assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
        await db.commit()


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_the_lenders_machine_from_its_household_ends_the_link(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await mesh_router.remove_household_machine(w.lender.node_id, db=db, member_id=w.lender.member_id)
    await _removed_on_both(w, out.link_id, "revoked")
    assert await w.row(out.link_id) is None, "the row goes with the machine"


@pytest.mark.asyncio
async def test_deleting_the_requesters_node_ends_the_link(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        await mesh_router.delete_mesh_node(
            w.requester.node_id, w.requester.wg_key, auth_node=None, db=db)
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_another_machine_leaving_the_household_leaves_the_link_alone(make_world) -> None:
    w = await make_world()
    out = await create(w)
    async with w.factory() as db:
        from coordinator.models import MeshNode
        spare = MeshNode(household_id=w.requester.household_id, node_name="spare",
                         mesh_ip="100.127.250.9", wg_public_key="spare-" + uuid.uuid4().hex + "=" * 8)
        db.add(spare)
        await db.commit()
        spare_id = spare.id
    async with w.factory() as db:
        await mesh_router.remove_household_machine(spare_id, db=db, member_id=w.member_id)
    assert (await w.row(out.link_id)).ended_at is None
    assert w.lender.sidecar.of_type("circle_peer_remove") == []


# ---------------------------------------------------------------------------
# The lender's engine: sharing and disconnects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lender_that_stops_sharing_with_circles_ends_its_links_at_once(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await circle_links.on_peer_registered(
        w.lender.peer_id, serves_circle=False, mesh_node_id=w.lender.node_id, circle_port=8347)
    assert (await w.row(out.link_id)).end_reason == "sharing_off"
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_a_lender_that_reconnects_unchanged_keeps_its_links(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await circle_links.on_peer_registered(
        w.lender.peer_id, serves_circle=True, mesh_node_id=w.lender.node_id, circle_port=8347)
    assert (await w.row(out.link_id)).ended_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id,port", [
    ("other", 8347),   # now answers on a different machine
    ("same", 9000),    # same machine, new circle port: the filter entry is stale
    (None, None),      # came back unbound
])
async def test_a_lender_that_comes_back_somewhere_else_loses_its_links(make_world, node_id, port) -> None:
    w = await make_world()
    out = await create(w)
    node = {"other": uuid.uuid4(), "same": w.lender.node_id, None: None}[node_id]
    await circle_links.on_peer_registered(
        w.lender.peer_id, serves_circle=True, mesh_node_id=node, circle_port=port)
    assert (await w.row(out.link_id)).end_reason == "lender_gone"


@pytest.mark.asyncio
async def test_a_lender_disconnect_ends_its_links_after_the_grace(make_world, links_on) -> None:
    w = await make_world()
    links_on.setattr(circle_links, "LENDER_DISCONNECT_GRACE_S", 0.8)
    out = await create(w)
    await unregister_peer(w.lender.peer_id)
    circle_links.on_peer_gone(w.lender.peer_id, w.lender.node_id)
    await asyncio.sleep(0.1)
    assert (await w.row(out.link_id)).ended_at is None, "still inside the grace"
    await asyncio.sleep(1.5)
    assert (await w.row(out.link_id)).end_reason == "lender_gone"
    await _removed_on_both(w, out.link_id, "revoked")
    assert w.lender.peer_id not in circle_links._grace_tasks


@pytest.mark.asyncio
async def test_a_lender_that_reconnects_inside_the_grace_keeps_its_links(make_world, links_on) -> None:
    w = await make_world()
    links_on.setattr(circle_links, "LENDER_DISCONNECT_GRACE_S", 0.8)
    out = await create(w)
    await unregister_peer(w.lender.peer_id)
    circle_links.on_peer_gone(w.lender.peer_id, w.lender.node_id)
    await asyncio.sleep(0.1)
    await register_peer(PeerConnection(
        peer_id=w.lender.peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=w.lender.member_id,
        mesh_node_id=w.lender.node_id, circle_port=8347))
    await circle_links.on_peer_registered(
        w.lender.peer_id, serves_circle=True, mesh_node_id=w.lender.node_id, circle_port=8347)
    await asyncio.sleep(1.4)  # well past the grace it would have had
    assert (await w.row(out.link_id)).ended_at is None
    assert w.lender.peer_id not in circle_links._grace_tasks


@pytest.mark.asyncio
async def test_the_grace_does_not_end_links_of_a_lender_that_is_back_without_the_hook(make_world, links_on) -> None:
    w = await make_world()
    links_on.setattr(circle_links, "LENDER_DISCONNECT_GRACE_S", 0.2)
    out = await create(w)
    circle_links.on_peer_gone(w.lender.peer_id, w.lender.node_id)  # but the peer never left the registry
    await asyncio.sleep(0.5)
    assert (await w.row(out.link_id)).ended_at is None


@pytest.mark.asyncio
async def test_an_engine_with_no_binding_starts_no_timer(make_world) -> None:
    w = await make_world()
    circle_links.on_peer_gone(w.lender.peer_id, None)
    assert w.lender.peer_id not in circle_links._grace_tasks


@pytest.mark.asyncio
async def test_the_hooks_do_nothing_when_links_are_switched_off(make_world, links_on) -> None:
    w = await make_world()
    out = await create(w)
    links_on.setenv("POTLUCK_CIRCLE_LINKS_ENABLED", "0")
    from coordinator.config import get_settings
    get_settings.cache_clear()
    circle_links.on_peer_gone(w.lender.peer_id, w.lender.node_id)
    assert w.lender.peer_id not in circle_links._grace_tasks
    await circle_links.on_peer_registered(
        w.lender.peer_id, serves_circle=False, mesh_node_id=None, circle_port=None)
    assert (await w.row(out.link_id)).ended_at is None
    await circle_links.replay_for_node(w.lender.node_id)
    assert len(w.lender.sidecar.of_type("circle_peer_add")) == 1, "no replay"


# ---------------------------------------------------------------------------
# The sweeper
# ---------------------------------------------------------------------------


async def _age(w, link_id, *, expires="now() - interval '1 second'", created="now() - interval '11 minutes'",
               ended=None, reason=None):
    sets = f"expires_at = {expires}, created_at = {created}"
    if ended:
        sets += f", ended_at = {ended}, end_reason = '{reason}'"
    async with w.factory() as db:
        await db.execute(text(f"UPDATE circle_links SET {sets} WHERE id = :i"), {"i": link_id})
        await db.commit()


@pytest.mark.asyncio
async def test_the_sweeper_ends_a_lapsed_link_and_tells_both_sidecars(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await _age(w, out.link_id)
    swept = await circle_links.sweep_once(w.factory)
    assert swept.expired == 1
    assert (await w.row(out.link_id)).end_reason == "expired"
    await _removed_on_both(w, out.link_id, "expired")
    again = await circle_links.sweep_once(w.factory)
    assert (again.expired, again.reconciled, again.deleted) == (0, 0, 0)
    assert len(w.lender.sidecar.of_type("circle_peer_remove")) == 1


@pytest.mark.asyncio
async def test_the_sweeper_leaves_live_links_alone(make_world) -> None:
    w = await make_world()
    out = await create(w)
    swept = await circle_links.sweep_once(w.factory)
    assert (swept.expired, swept.reconciled, swept.deleted) == (0, 0, 0)
    assert (await w.row(out.link_id)).ended_at is None
    assert w.lender.sidecar.of_type("circle_peer_remove") == []


@pytest.mark.asyncio
async def test_rows_are_deleted_a_day_after_the_link_ended(make_world) -> None:
    w = await make_world(lenders=0)
    a = await w.add_lender(tokens_per_second=90.0)
    b = await w.add_lender(tokens_per_second=50.0)
    c = await w.add_lender(tokens_per_second=10.0)
    old = await create(w)
    recent = await create(w, exclude=[old.link_id])
    live = await create(w, exclude=[old.link_id, recent.link_id])
    await end(w, old.link_id)
    await end(w, recent.link_id)
    await _age(w, old.link_id, ended="now() - interval '25 hours'", reason="ended")
    await _age(w, recent.link_id, ended="now() - interval '23 hours'", reason="ended")
    swept = await circle_links.sweep_once(w.factory)
    assert swept.deleted == 1
    assert await w.row(old.link_id) is None
    assert await w.row(recent.link_id) is not None
    assert (await w.row(live.link_id)).ended_at is None


@pytest.mark.asyncio
async def test_the_sweeper_ends_links_the_circle_no_longer_allows(make_world) -> None:
    w = await make_world(requester_owns_circle=False)
    out = await create(w)
    async with w.factory() as db:  # an edit made outside the routes
        await db.execute(text("DELETE FROM circle_memberships WHERE circle_id = :c AND member_id = :m"),
                         {"c": w.circle_id, "m": w.member_id})
        await db.commit()
    assert (await circle_links.sweep_once(w.factory)).reconciled == 1
    assert (await w.row(out.link_id)).end_reason == "member_left"
    await _removed_on_both(w, out.link_id, "revoked")


@pytest.mark.asyncio
async def test_the_sweeper_ends_links_whose_lender_is_gone_with_no_timer_running(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await unregister_peer(w.lender.peer_id)         # gone, and no grace timer (a restart lost it)
    assert (await circle_links.sweep_once(w.factory)).reconciled == 1
    assert (await w.row(out.link_id)).end_reason == "lender_gone"


@pytest.mark.asyncio
async def test_the_sweeper_waits_for_lenders_to_reconnect_after_a_restart(make_world, links_on) -> None:
    w = await make_world()
    out = await create(w)
    links_on.setattr(circle_links, "_PROCESS_STARTED", time.monotonic())  # just booted
    await unregister_peer(w.lender.peer_id)
    assert (await circle_links.sweep_once(w.factory)).reconciled == 0
    assert (await w.row(out.link_id)).ended_at is None


@pytest.mark.asyncio
async def test_the_sweeper_ends_links_of_a_lender_that_stopped_sharing(make_world) -> None:
    from coordinator.services import peer_registry

    w = await make_world()
    out = await create(w)
    peer_registry._peers[w.lender.peer_id].serves_circle = False
    assert (await circle_links.sweep_once(w.factory)).reconciled == 1
    assert (await w.row(out.link_id)).end_reason == "sharing_off"


@pytest.mark.asyncio
async def test_the_sweeper_loop_runs_until_told_to_stop(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await _age(w, out.link_id)
    stop = asyncio.Event()
    task = asyncio.create_task(circle_links.run_sweeper_loop(stop, interval_s=0.05))
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if (await w.row(out.link_id)).ended_at is not None:
                break
        assert (await w.row(out.link_id)).end_reason == "expired"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_a_failing_sweep_pass_does_not_stop_the_loop(make_world, links_on) -> None:
    w = await make_world()
    calls = {"n": 0}
    real = circle_links.sweep_once

    async def flaky(factory):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return await real(factory)

    links_on.setattr(circle_links, "sweep_once", flaky)
    stop = asyncio.Event()
    task = asyncio.create_task(circle_links.run_sweeper_loop(stop, interval_s=0.05))
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_the_sweeper_forgets_idle_counters(make_world) -> None:
    w = await make_world()
    circle_links._create_attempts[uuid.uuid4()] = __import__("collections").deque()
    circle_links._create_locks[uuid.uuid4()] = asyncio.Lock()
    await circle_links.sweep_once(w.factory)
    assert circle_links._create_attempts == {} and circle_links._create_locks == {}


# ---------------------------------------------------------------------------
# Replay when a sidecar reconnects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reconnecting_sidecar_is_sent_its_live_links_again(make_world) -> None:
    w = await make_world()
    out = await create(w)
    await asyncio.sleep(0.05)
    w.lender.sidecar.sent.clear()
    w.requester.sidecar.sent.clear()
    await circle_links.replay_for_node(w.lender.node_id)
    (lender_add,) = w.lender.sidecar.sent
    assert lender_add["type"] == "circle_peer_add" and lender_add["role"] == "lender"
    assert lender_add["link_id"] == str(out.link_id)
    assert lender_add["peer_wg_public_key"] == w.requester.wg_key
    assert 595 <= lender_add["ttl_s"] <= 600, "the remaining lifetime"
    import hashlib
    assert lender_add["token_sha256"] == hashlib.sha256(out.token.encode()).hexdigest()
    assert w.requester.sidecar.sent == [], "only the node that reconnected"

    await circle_links.replay_for_node(w.requester.node_id)
    (requester_add,) = w.requester.sidecar.sent
    assert requester_add["role"] == "requester" and requester_add["token_sha256"] is None


@pytest.mark.asyncio
async def test_replay_skips_ended_lapsed_and_nearly_lapsed_links(make_world) -> None:
    w = await make_world(lenders=0)
    for tps in (90.0, 50.0, 10.0):
        await w.add_lender(tokens_per_second=tps)
    ended = await create(w)
    lapsed = await create(w, exclude=[ended.link_id])
    nearly = await create(w, exclude=[ended.link_id, lapsed.link_id])
    await end(w, ended.link_id)
    await _age(w, lapsed.link_id)
    await _age(w, nearly.link_id, expires="now() + interval '3 seconds'")
    for m in w.lenders:
        m.sidecar.sent.clear()
    for m in w.lenders:
        await circle_links.replay_for_node(m.node_id)
    # No add goes out for any of the three. The one that ended is removed again (its
    # lender may have missed the first); the lapsed and the nearly lapsed are left to
    # the sweeper and to the sidecar's own clock.
    first, second, third = w.lenders
    assert [m["type"] for m in first.sidecar.sent] == ["circle_peer_remove"]
    assert second.sidecar.sent == [] and third.sidecar.sent == []


@pytest.mark.asyncio
async def test_replay_does_not_go_to_a_sidecar_that_never_advertised_links(make_world) -> None:
    from coordinator.routers import mesh

    w = await make_world()
    await create(w)
    w.lender.sidecar.sent.clear()
    mesh._signal_features[w.lender.node_id] = frozenset()
    await circle_links.replay_for_node(w.lender.node_id)
    assert w.lender.sidecar.sent == []


@pytest.mark.asyncio
async def test_a_removal_missed_while_the_sidecar_was_off_is_repeated_when_it_reconnects(make_world) -> None:
    """The lender's sidecar was off the signal channel when the link was revoked,
    so it never heard. When it comes back it is told again, for as long as it
    could still be holding the grant."""
    w = await make_world(lenders=0)
    a = await w.add_lender(tokens_per_second=90.0)
    b = await w.add_lender(tokens_per_second=50.0)
    gone = await create(w)
    kept = await create(w, exclude=[gone.link_id])
    assert gone.lender.mesh_ip == a.mesh_ip and kept.lender.mesh_ip == b.mesh_ip

    from coordinator.routers import mesh
    mesh._signal_connections.pop(a.node_id)          # sidecar A drops off the mesh
    await end(w, gone.link_id)                        # revoked while it is away
    assert a.sidecar.of_type("circle_peer_remove") == []
    mesh._signal_connections[a.node_id] = a.sidecar
    a.sidecar.sent.clear()
    await circle_links.replay_for_node(a.node_id)
    (remove,) = a.sidecar.sent
    assert remove == {"type": "circle_peer_remove", "link_id": str(gone.link_id), "reason": "ended"}

    # An old enough end is not repeated; one that is not this node's is not sent.
    async with w.factory() as db:
        await db.execute(text("UPDATE circle_links SET ended_at = now() - interval '31 minutes' WHERE id = :i"),
                         {"i": gone.link_id})
        await db.commit()
    a.sidecar.sent.clear()
    b.sidecar.sent.clear()
    await circle_links.replay_for_node(a.node_id)
    assert a.sidecar.sent == []
    await circle_links.replay_for_node(b.node_id)
    assert [m["type"] for m in b.sidecar.sent] == ["circle_peer_add"], "B only gets its own live link"


@pytest.mark.asyncio
async def test_replay_sends_removes_before_adds(make_world) -> None:
    w = await make_world()
    first = await create(w)
    second = await create(w)                    # supersedes the first: same pair
    w.lender.sidecar.sent.clear()
    await circle_links.replay_for_node(w.lender.node_id)
    kinds = [(m["type"], m["link_id"]) for m in w.lender.sidecar.sent]
    assert kinds == [("circle_peer_remove", str(first.link_id)), ("circle_peer_add", str(second.link_id))]
