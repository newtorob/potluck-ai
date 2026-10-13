"""Peer-quality routing: the open pool must not route to a proven-bad peer.

A peer's reputation hard-resets to 0.0 on a wrong-answer canary (fabrication).
pick_peer_for_model(enforce_pool_trust=True) drops any peer below the re-entry
bar (NEUTRAL 0.5), so a fallen peer must climb the whole 0.0 -> NEUTRAL band back
before it can serve strangers again (hysteresis: one clean pass off the floor is
not enough). My Machines routing is never gated, and healthy / unrated peers are
unaffected.

Registers in-memory peers (the registry is a module global, so each test
unregisters in a finally) and uses a unique model slug so live DB peers can't
match the capability filter.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.services.peer_registry import (
    PeerConnection,
    pick_peer_for_model,
    register_peer,
    unregister_peer,
)

_MODEL = "qwen2.5:3b-quality-test"  # unique: no live peer advertises this


class _WS:  # pick_peer_for_model stores but never calls the ws; a stub suffices
    pass


async def _mk_peer(
    db: AsyncSession, member_id: uuid.UUID, *, reputation: float | None
) -> uuid.UUID:
    peer = Peer(
        member_id=member_id,
        public_key=uuid.uuid4().hex,
        hardware_summary={},
        status="active",
    )
    db.add(peer)
    await db.flush()
    db.add(PeerCapability(peer_id=peer.id, model_slug=_MODEL, quantization="q4_K_M"))
    if reputation is not None:
        db.add(
            PeerHeartbeat(
                peer_id=peer.id,
                last_beat_at=datetime.now(timezone.utc),
                reputation=reputation,
                binary_hash="testhash",
                client_version="0.0.0",
            )
        )
    await db.flush()
    await register_peer(
        PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(), member_id=member_id)
    )
    return peer.id


@pytest.mark.asyncio
async def test_pool_excludes_floor_reputation_peer(db_session: AsyncSession) -> None:
    member = uuid.uuid4()
    bad = await _mk_peer(db_session, member, reputation=0.0)
    try:
        # Open pool: the floor-reputation peer is excluded -> nothing to serve.
        assert await pick_peer_for_model(db_session, _MODEL, enforce_pool_trust=True) is None
        # My Machines: never gated -- the owner still gets their own machine.
        mine = await pick_peer_for_model(db_session, _MODEL, owner_member_id=member)
        assert mine is not None and mine.peer_id == bad
        # Default (un-gated) path also returns it.
        any_peer = await pick_peer_for_model(db_session, _MODEL)
        assert any_peer is not None and any_peer.peer_id == bad
    finally:
        await unregister_peer(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("reputation", [0.1, 0.4])
async def test_pool_excludes_peer_in_quarantine_band(
    db_session: AsyncSession, reputation: float
) -> None:
    # Hysteresis: a peer climbing back from the floor but still below NEUTRAL is
    # not yet allowed into the pool -- one or two clean passes off 0.0 must not
    # re-admit a peer that was proven bad.
    member = uuid.uuid4()
    recovering = await _mk_peer(db_session, member, reputation=reputation)
    try:
        assert await pick_peer_for_model(db_session, _MODEL, enforce_pool_trust=True) is None
        # Still reachable on My Machines (never gated) and the default path.
        mine = await pick_peer_for_model(db_session, _MODEL, owner_member_id=member)
        assert mine is not None and mine.peer_id == recovering
    finally:
        await unregister_peer(recovering)


@pytest.mark.asyncio
async def test_pool_keeps_healthy_peer(db_session: AsyncSession) -> None:
    # NEUTRAL (0.5) is the re-entry bar: a peer exactly at neutral is admitted.
    member = uuid.uuid4()
    good = await _mk_peer(db_session, member, reputation=0.5)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL, enforce_pool_trust=True)
        assert picked is not None and picked.peer_id == good
    finally:
        await unregister_peer(good)


@pytest.mark.asyncio
async def test_pool_keeps_unrated_peer(db_session: AsyncSession) -> None:
    # A peer with no heartbeat row yet has no reputation record -> treated as
    # neutral and kept (a brand-new peer must be able to earn its first job).
    member = uuid.uuid4()
    unrated = await _mk_peer(db_session, member, reputation=None)
    try:
        picked = await pick_peer_for_model(db_session, _MODEL, enforce_pool_trust=True)
        assert picked is not None and picked.peer_id == unrated
    finally:
        await unregister_peer(unrated)
