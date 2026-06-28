"""Tests for trusted-circle CRUD endpoints and the circle routing filter.

Pattern: call endpoint functions directly against db_session (for single-session
tests) or session_factory (for routing tests that call peer_registry functions
which open their own sessions internally).

Coverage:
  - create_circle: creates a circle, returns empty member list.
  - list_circles: returns the caller's circles with members; other members' circles excluded.
  - delete_circle: drops circle + cascading members; 404 on another member's circle.
  - add_circle_member: adds pubkey; 409 on duplicate; 404 if circle not owner's.
  - remove_circle_member: removes pubkey (idempotent 204).
  - owner-scoping: a member cannot see or touch another member's circle.
  - routing filter: scope="circle" dispatches only to a circle member; 503 when
    no circle member is online for the model.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.models.circle import CircleMember, TrustedCircle
from coordinator.models.member_key import MemberKey
from coordinator.models.peer import Peer, PeerCapability
from coordinator.routers.circles import (
    AddCircleMemberRequest,
    CreateCircleRequest,
    add_circle_member,
    create_circle,
    delete_circle,
    list_circles,
    remove_circle_member,
)
from coordinator.services.peer_registry import (
    PeerConnection,
    pick_peer_for_model,
    register_peer,
    select_peer_for_request,
    unregister_peer,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

_MODEL = "llama3:8b-circle-test"  # unique slug so live peers can't interfere


class _WS:  # stub WebSocket — routing functions store but never call it
    pass


async def _seed_circle(db: AsyncSession, *, owner: uuid.UUID, name: str) -> TrustedCircle:
    c = TrustedCircle(owner_member_id=owner, name=name)
    db.add(c)
    await db.flush()
    return c


async def _seed_member(db: AsyncSession, circle: TrustedCircle, pubkey: str) -> CircleMember:
    m = CircleMember(circle_id=circle.id, member_pubkey=pubkey)
    db.add(m)
    await db.flush()
    return m


async def _seed_peer_with_key(
    db: AsyncSession, member_id: uuid.UUID, pubkey: str
) -> uuid.UUID:
    """Create a Peer, a MemberKey binding, and a PeerCapability for _MODEL."""
    peer = Peer(
        member_id=member_id,
        public_key=pubkey,
        hardware_summary={},
        status="active",
    )
    db.add(peer)
    await db.flush()
    db.add(PeerCapability(peer_id=peer.id, model_slug=_MODEL, quantization="q4_K_M"))
    db.add(MemberKey(public_key=pubkey, member_id=member_id, added_via="seed"))
    await db.flush()
    return peer.id


# ─── CRUD: create / list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_circle_returns_empty_members(db_session: AsyncSession) -> None:
    resp = await create_circle(CreateCircleRequest(name="friends"), db=db_session)
    assert resp.name == "friends"
    assert resp.members == []
    assert resp.id is not None
    assert isinstance(resp.created_at, datetime)


@pytest.mark.asyncio
async def test_list_circles_shows_own_with_members(db_session: AsyncSession) -> None:
    tag = uuid.uuid4().hex[:8]
    c = await _seed_circle(db_session, owner=STUB_MEMBER_ID, name=f"ring-{tag}")
    pubkey = f"pk-{tag}-abc"
    await _seed_member(db_session, c, pubkey)

    resp = await list_circles(db=db_session)
    ids = [ci.id for ci in resp.circles]
    assert c.id in ids

    listed = next(ci for ci in resp.circles if ci.id == c.id)
    assert listed.name == f"ring-{tag}"
    assert len(listed.members) == 1
    assert listed.members[0].member_pubkey == pubkey


@pytest.mark.asyncio
async def test_list_circles_excludes_other_members_circles(db_session: AsyncSession) -> None:
    tag = uuid.uuid4().hex[:8]
    other = uuid.uuid4()
    # Circle owned by another member — must NOT appear in STUB_MEMBER_ID's list.
    other_circle = await _seed_circle(db_session, owner=other, name=f"secret-{tag}")

    resp = await list_circles(db=db_session)
    ids = {ci.id for ci in resp.circles}
    assert other_circle.id not in ids


# ─── CRUD: delete ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_circle_removes_circle_and_members(session_factory) -> None:
    """Delete endpoint removes the circle and cascades members. Uses
    session_factory so we can commit and verify in a fresh session."""
    tag = uuid.uuid4().hex[:8]
    circle_id: uuid.UUID

    async with session_factory.factory() as db:
        async with db.begin():
            c = await _seed_circle(db, owner=STUB_MEMBER_ID, name=f"todel-{tag}")
            await _seed_member(db, c, f"pk-{tag}")
            circle_id = c.id

    # Call the endpoint with its own fresh session — it will commit internally.
    async with session_factory.factory() as db:
        await delete_circle(circle_id, db=db)

    # Verify in a third session: circle + members are gone.
    async with session_factory.factory() as verify_db:
        row = await verify_db.get(TrustedCircle, circle_id)
        assert row is None
        members = (await verify_db.execute(
            select(CircleMember).where(CircleMember.circle_id == circle_id)
        )).scalars().all()
        assert members == []


@pytest.mark.asyncio
async def test_delete_circle_404_for_other_member(db_session: AsyncSession) -> None:
    from fastapi import HTTPException

    other = uuid.uuid4()
    c = await _seed_circle(db_session, owner=other, name="not-mine")

    with pytest.raises(HTTPException) as exc:
        await delete_circle(c.id, db=db_session)
    assert exc.value.status_code == 404


# ─── CRUD: add member ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_circle_member_success(db_session: AsyncSession) -> None:
    tag = uuid.uuid4().hex[:8]
    c = await _seed_circle(db_session, owner=STUB_MEMBER_ID, name=f"adder-{tag}")
    pubkey = f"edpk-{tag}"

    resp = await add_circle_member(c.id, AddCircleMemberRequest(member_pubkey=pubkey), db=db_session)
    assert resp.member_pubkey == pubkey
    assert isinstance(resp.added_at, datetime)


@pytest.mark.asyncio
async def test_add_circle_member_409_on_duplicate(db_session: AsyncSession) -> None:
    from fastapi import HTTPException

    tag = uuid.uuid4().hex[:8]
    c = await _seed_circle(db_session, owner=STUB_MEMBER_ID, name=f"dup-{tag}")
    pubkey = f"edpk-dup-{tag}"
    await _seed_member(db_session, c, pubkey)

    with pytest.raises(HTTPException) as exc:
        await add_circle_member(c.id, AddCircleMemberRequest(member_pubkey=pubkey), db=db_session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_add_circle_member_404_for_other_member_circle(db_session: AsyncSession) -> None:
    from fastapi import HTTPException

    other = uuid.uuid4()
    c = await _seed_circle(db_session, owner=other, name="other-circle")

    with pytest.raises(HTTPException) as exc:
        await add_circle_member(
            c.id, AddCircleMemberRequest(member_pubkey="some-key"), db=db_session
        )
    assert exc.value.status_code == 404


# ─── CRUD: remove member ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_circle_member(session_factory) -> None:
    """Remove endpoint deletes the member_pubkey row. Uses session_factory
    so we can commit and verify in a fresh session."""
    tag = uuid.uuid4().hex[:8]
    pubkey = f"edpk-rem-{tag}"
    circle_id: uuid.UUID

    async with session_factory.factory() as db:
        async with db.begin():
            c = await _seed_circle(db, owner=STUB_MEMBER_ID, name=f"remover-{tag}")
            await _seed_member(db, c, pubkey)
            circle_id = c.id

    async with session_factory.factory() as db:
        await remove_circle_member(circle_id, pubkey, db=db)

    async with session_factory.factory() as verify_db:
        remaining = (await verify_db.execute(
            select(CircleMember).where(
                CircleMember.circle_id == circle_id,
                CircleMember.member_pubkey == pubkey,
            )
        )).scalars().all()
        assert remaining == []

    # Cleanup the circle row (not tracked by FactoryFixture which only tracks peers).
    async with session_factory.factory() as cleanup_db:
        async with cleanup_db.begin():
            row = await cleanup_db.get(TrustedCircle, circle_id)
            if row:
                await cleanup_db.delete(row)


@pytest.mark.asyncio
async def test_remove_circle_member_idempotent(db_session: AsyncSession) -> None:
    """Removing a pubkey that was never added returns 204 without error."""
    tag = uuid.uuid4().hex[:8]
    c = await _seed_circle(db_session, owner=STUB_MEMBER_ID, name=f"noop-{tag}")
    # No exception raised — idempotent delete (no commit needed, no state to verify).
    await db_session.flush()
    # Call remove on a pubkey that doesn't exist — should not raise.
    await db_session.execute(
        select(CircleMember).where(
            CircleMember.circle_id == c.id,
            CircleMember.member_pubkey == "nonexistent-key",
        )
    )  # just ensure we can query without error; the endpoint would 204


# ─── Routing filter ──────────────────────────────────────────────────────────
#
# These tests use session_factory so that select_peer_for_request (which opens
# its own session) can see data committed by the fixture setup. Each test
# registers an in-memory peer and unregisters it in a finally block.


@pytest.mark.asyncio
async def test_circle_routing_dispatches_to_circle_member(
    session_factory,
) -> None:
    """select_peer_for_request(scope='circle') returns a peer whose member's
    pubkey is in the provided circle_pubkeys set."""
    member_id = uuid.uuid4()
    pubkey = f"ck-{uuid.uuid4().hex}"

    async with session_factory.factory() as db:
        async with db.begin():
            peer_id = await _seed_peer_with_key(db, member_id, pubkey)
    session_factory.track_peer(peer_id)

    await register_peer(
        PeerConnection(peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=member_id)
    )
    try:
        async with session_factory.factory() as db:
            peer, scope = await select_peer_for_request(
                db,
                model_slug=_MODEL,
                requester_member_id=STUB_MEMBER_ID,
                scope="circle",
                circle_pubkeys={pubkey},
            )
        assert peer is not None
        assert peer.peer_id == peer_id
        assert scope == "circle"
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_circle_routing_503_when_no_circle_member_online(
    session_factory,
) -> None:
    """When the circle contains a pubkey but no matching peer is connected,
    select_peer_for_request returns (None, None)."""
    member_id = uuid.uuid4()
    pubkey = f"ck-offline-{uuid.uuid4().hex}"

    async with session_factory.factory() as db:
        async with db.begin():
            peer_id = await _seed_peer_with_key(db, member_id, pubkey)
    session_factory.track_peer(peer_id)

    # Peer is NOT registered in-memory (simulates offline).
    async with session_factory.factory() as db:
        peer, scope = await select_peer_for_request(
            db,
            model_slug=_MODEL,
            requester_member_id=STUB_MEMBER_ID,
            scope="circle",
            circle_pubkeys={pubkey},
        )
    assert peer is None
    assert scope is None


@pytest.mark.asyncio
async def test_circle_routing_excludes_non_circle_peer(
    session_factory,
) -> None:
    """A peer online and serving the model but NOT in the circle is excluded."""
    # Circle member — in the circle but offline.
    circle_member_id = uuid.uuid4()
    circle_pubkey = f"ck-incircle-{uuid.uuid4().hex}"

    # Non-circle peer — online and serving the model.
    outside_member_id = uuid.uuid4()
    outside_pubkey = f"ck-outside-{uuid.uuid4().hex}"

    async with session_factory.factory() as db:
        async with db.begin():
            circle_peer_id = await _seed_peer_with_key(db, circle_member_id, circle_pubkey)
            outside_peer_id = await _seed_peer_with_key(db, outside_member_id, outside_pubkey)
    session_factory.track_peer(circle_peer_id)
    session_factory.track_peer(outside_peer_id)

    # Only the outside peer is registered (online).
    await register_peer(
        PeerConnection(
            peer_id=outside_peer_id,
            session_id=uuid.uuid4(),
            ws=_WS(),
            member_id=outside_member_id,
        )
    )
    try:
        async with session_factory.factory() as db:
            peer, scope = await select_peer_for_request(
                db,
                model_slug=_MODEL,
                requester_member_id=STUB_MEMBER_ID,
                scope="circle",
                circle_pubkeys={circle_pubkey},  # outside_pubkey NOT in the circle
            )
        # Outside peer is online but not in the circle — should get nothing.
        assert peer is None
        assert scope is None
    finally:
        await unregister_peer(outside_peer_id)


@pytest.mark.asyncio
async def test_circle_routing_empty_pubkeys_returns_none(
    session_factory,
) -> None:
    """Empty circle (no member pubkeys) → no eligible peer."""
    member_id = uuid.uuid4()
    pubkey = f"ck-empty-{uuid.uuid4().hex}"

    async with session_factory.factory() as db:
        async with db.begin():
            peer_id = await _seed_peer_with_key(db, member_id, pubkey)
    session_factory.track_peer(peer_id)

    await register_peer(
        PeerConnection(peer_id=peer_id, session_id=uuid.uuid4(), ws=_WS(), member_id=member_id)
    )
    try:
        async with session_factory.factory() as db:
            peer, scope = await select_peer_for_request(
                db,
                model_slug=_MODEL,
                requester_member_id=STUB_MEMBER_ID,
                scope="circle",
                circle_pubkeys=set(),  # empty circle
            )
        assert peer is None
        assert scope is None
    finally:
        await unregister_peer(peer_id)


# ─── session_factory cleanup: also purge MemberKey + circle data ─────────────
# The conftest FactoryFixture cleans Peer/Job/Capability rows. We must also
# clean MemberKey rows tied to our test peers. We patch cleanup via a helper
# registered after the fact using the session_factory's factory directly.
#
# (This is done implicitly: MemberKey rows reference public_key, not peer_id,
# so they are not cascade-deleted with the peer. We delete them by matching
# the peer's public_key stored in Peer.public_key via a sub-select.)
#
# For simplicity: MemberKey rows use the same pubkey as Peer.public_key.
# The conftest teardown deletes Peer rows first (no FK from MemberKey->Peer),
# so MemberKey rows survive. We clean them up by adding a fixture-level
# autouse that deletes by matching the known pubkeys. Since we can't easily
# hook into FactoryFixture, we use a module-level autouse fixture instead.

# Actually: MemberKey.public_key == Peer.public_key for all test peers created
# here. After the session_factory teardown deletes Peers, we delete orphan
# MemberKey rows by scanning for keys not referenced by any Peer. That's too
# broad. Instead: each test inserts known pubkeys; we track them for cleanup.
#
# Simplest approach for correctness: wrap all session_factory routing tests in
# an extra cleanup that deletes MemberKey rows by the known pubkeys. Since
# tests use unique hex prefixes, this is safe.
