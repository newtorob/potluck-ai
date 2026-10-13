"""Who a contributor agreed to serve.

A peer says at attestation whether it serves trusted circles, the open pool,
or both. Without this, every connected peer would be an open-pool candidate,
so a member could not lend a machine to friends without lending it to
everyone.
"""
from __future__ import annotations

import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from coordinator.services.peer_registry import (
    PeerConnection,
    circle_model_availability,
    honoured_audiences,
    register_peer,
    select_peer_for_request,
    unregister_peer,
)
from coordinator.models.member_key import MemberKey
from coordinator.models.peer import Peer, PeerCapability
from tests.test_circles import _WS
from tests.test_contributor_m25 import _attest, _cleanup, _make_app, _pubkey


async def _online(session_factory, **flags):
    """One member's machine, connected, offering a model nobody else has."""
    member_id = uuid.uuid4()
    pubkey = f"aud-{uuid.uuid4().hex}"
    model = f"aud-model-{uuid.uuid4().hex[:8]}"
    async with session_factory.factory() as db:
        async with db.begin():
            peer = Peer(member_id=member_id, public_key=pubkey, hardware_summary={}, status="active")
            db.add(peer)
            await db.flush()
            db.add(PeerCapability(peer_id=peer.id, model_slug=model, quantization="q4"))
            db.add(MemberKey(public_key=pubkey, member_id=member_id, added_via="seed"))
    session_factory.track_peer(peer.id)
    await register_peer(
        PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(),
                       member_id=member_id, **flags)
    )
    return member_id, pubkey, peer.id, model


async def _pick(session_factory, *, requester, scope, circle_pubkeys=None, model):
    async with session_factory.factory() as db:
        return await select_peer_for_request(
            db, model_slug=model, requester_member_id=requester, scope=scope,
            circle_pubkeys=circle_pubkeys,
        )


def test_honoured_audiences_keeps_known_names_in_a_fixed_order() -> None:
    assert honoured_audiences(None) is None
    assert honoured_audiences(["pool", "circle"]) == ["circle", "pool"]
    assert honoured_audiences(["circle"]) == ["circle"]
    assert honoured_audiences(["circle", "something-newer"]) == ["circle"]
    assert honoured_audiences([]) == []


@pytest.mark.asyncio
async def test_circle_only_peer_is_never_an_open_pool_candidate(session_factory) -> None:
    member_id, pubkey, peer_id, model = await _online(session_factory, serves_pool=False)
    try:
        stranger = uuid.uuid4()
        assert await _pick(session_factory, requester=stranger, scope="pool", model=model) == (None, None)
        peer, scope = await _pick(session_factory, requester=stranger, scope="circle",
                                  circle_pubkeys={pubkey}, model=model)
        assert peer is not None and peer.peer_id == peer_id and scope == "circle"
        # Its owner's own requests are always served, whatever the audience.
        peer, scope = await _pick(session_factory, requester=member_id, scope="pool", model=model)
        assert peer is not None and peer.peer_id == peer_id and scope == "mine"
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_pool_only_peer_is_not_offered_to_a_circle(session_factory) -> None:
    member_id, pubkey, peer_id, model = await _online(session_factory, serves_circle=False)
    try:
        stranger = uuid.uuid4()
        assert await _pick(session_factory, requester=stranger, scope="circle",
                           circle_pubkeys={pubkey}, model=model) == (None, None)
        peer, scope = await _pick(session_factory, requester=stranger, scope="pool", model=model)
        assert peer is not None and peer.peer_id == peer_id and scope == "pool"
    finally:
        await unregister_peer(peer_id)


@pytest.mark.asyncio
async def test_peer_that_named_no_audience_serves_both_as_before(session_factory) -> None:
    member_id, pubkey, peer_id, model = await _online(session_factory)
    try:
        stranger = uuid.uuid4()
        assert (await _pick(session_factory, requester=stranger, scope="pool", model=model))[1] == "pool"
        assert (await _pick(session_factory, requester=stranger, scope="circle",
                            circle_pubkeys={pubkey}, model=model))[1] == "circle"
    finally:
        await unregister_peer(peer_id)


# ---- the handshake carries the choice and echoes what is enforced -----------


@pytest.mark.parametrize("asked,echoed,circle,pool", [
    (None, None, True, True),
    (["circle"], ["circle"], True, False),
    (["pool", "circle"], ["circle", "pool"], True, True),
    (["newer-audience"], [], False, False),
])
def test_attestation_audiences_are_echoed_and_bound_to_the_connection(
    monkeypatch, asked, echoed, circle, pool
) -> None:
    from coordinator.services import peer_registry

    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    priv = Ed25519PrivateKey.generate()
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        try:
            with client.websocket_connect("/peers/connect") as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex, audiences=asked)
                assert resp["type"] == "accept", resp
                assert resp.get("audiences") == echoed
                conn = client.portal.call(lambda: peer_registry.get_peer(uuid.UUID(resp["peer_id"])))
                assert conn is not None
                assert (conn.serves_circle, conn.serves_pool) == (circle, pool)
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))


# ---- what a circle can run right now ----------------------------------------


@pytest.mark.asyncio
async def test_circle_availability_counts_machines_that_would_be_dispatched_to(session_factory) -> None:
    friend, friend_key, friend_peer, model = await _online(session_factory, serves_pool=False)
    pool_only, pool_only_key, pool_only_peer, pool_model = await _online(session_factory, serves_circle=False)
    _, outsider_key, outsider_peer, outsider_model = await _online(session_factory)
    try:
        async with session_factory.factory() as db:
            got = await circle_model_availability(db, {friend, pool_only, uuid.uuid4()})
            assert got == {model: 1}, "only members who are online and sharing with circles"
            assert await circle_model_availability(db, set()) == {}
            assert await circle_model_availability(db, {uuid.uuid4()}) == {}
        await unregister_peer(friend_peer)
        async with session_factory.factory() as db:
            assert await circle_model_availability(db, {friend}) == {}, "offline is not available"
    finally:
        for pid in (friend_peer, pool_only_peer, outsider_peer):
            await unregister_peer(pid)


@pytest.mark.asyncio
async def test_circle_availability_endpoint_is_for_people_in_the_circle_and_names_no_one(session_factory) -> None:
    from fastapi import HTTPException

    from coordinator.models.circle import CircleMember, CircleMembership, TrustedCircle
    from coordinator.routers.circles import circle_availability

    owner, stranger = uuid.uuid4(), uuid.uuid4()
    _, friend_key, friend_peer, model = await _online(session_factory)
    try:
        async with session_factory.factory() as db:
            circle = TrustedCircle(owner_member_id=owner, name=f"avail-{uuid.uuid4().hex[:6]}")
            db.add(circle)
            await db.flush()
            db.add(CircleMembership(circle_id=circle.id, member_id=owner, role="owner"))
            db.add(CircleMember(circle_id=circle.id, member_pubkey=friend_key))
            await db.commit()
            circle_id = circle.id
        try:
            async with session_factory.factory() as db:
                out = await circle_availability(circle_id, db=db, member_id=owner)
                assert out.model_dump(mode="json") == {
                    "circle_id": str(circle_id), "models": [{"slug": model, "machines": 1}],
                }
            async with session_factory.factory() as db:
                with pytest.raises(HTTPException) as exc:
                    await circle_availability(circle_id, db=db, member_id=stranger)
                assert exc.value.status_code == 404
        finally:
            async with session_factory.factory() as db:
                await db.delete(await db.get(TrustedCircle, circle_id))
                await db.commit()
    finally:
        await unregister_peer(friend_peer)
