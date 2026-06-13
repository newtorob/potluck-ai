"""Tests for the canary verification source + reputation-gated escrow release.

Requires a live Postgres via the db_session fixture (DB writes rolled back).
The in-memory peer/job registry is module-global, so tests that touch it clean
up after themselves.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.services.credit_ledger import (
    RELEASE_REPUTATION_THRESHOLD,
    record_job_credits,
    release_escrow_for_peer,
)
from coordinator.services.verification import (
    CANARIES,
    Canary,
    evaluate_canary,
    probe_due_peers,
    record_canary_result,
    run_canary_probe,
)

_MODEL = "qwen2.5:3b-instruct-q4_K_M"


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #


def test_evaluate_canary_accepts_correct_answer() -> None:
    c = Canary("What is 7 plus 5? Reply with only the number.", "12")
    assert evaluate_canary("12", c) is True
    assert evaluate_canary("  The answer is 12.  ", c) is True  # wrapped + spaced
    assert evaluate_canary("POTLUCK", Canary("...", "potluck")) is True  # case


def test_evaluate_canary_rejects_garbage() -> None:
    c = Canary("What is 7 plus 5? Reply with only the number.", "12")
    assert evaluate_canary("the the the the", c) is False
    assert evaluate_canary("", c) is False
    assert evaluate_canary("seven", c) is False


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


async def _peer(db: AsyncSession) -> Peer:
    p = Peer(member_id=uuid.uuid4(), public_key=f"pk-{uuid.uuid4()}")
    db.add(p)
    await db.flush()
    return p


async def _escrowed_job_for_peer(db: AsyncSession, peer_id: uuid.UUID) -> uuid.UUID:
    """A completed cross-member job served by `peer_id`, with its earned credit
    escrowed. Returns the job_id."""
    job = Job(member_id=uuid.uuid4(), model_slug=_MODEL, status="completed", peer_id=peer_id)
    db.add(job)
    await db.flush()
    _, earned = await record_job_credits(
        db,
        job_id=job.id,
        requester_member_id=uuid.uuid4(),
        contributor_owner_member_id=uuid.uuid4(),  # cross-member -> escrowed
        model_slug=_MODEL,
        output_tokens=100,
    )
    assert earned.escrowed is True
    return job.id


async def _is_escrowed(db: AsyncSession, job_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(CreditLedger.escrowed).where(
                CreditLedger.job_id == job_id, CreditLedger.direction == "earned"
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# release_escrow_for_peer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_release_escrow_for_peer_releases_only_that_peer(
    db_session: AsyncSession,
) -> None:
    peer_a = await _peer(db_session)
    peer_b = await _peer(db_session)
    job_a = await _escrowed_job_for_peer(db_session, peer_a.id)
    job_b = await _escrowed_job_for_peer(db_session, peer_b.id)

    released = await release_escrow_for_peer(db_session, peer_a.id)
    assert released == 1

    assert await _is_escrowed(db_session, job_a) is False, "peer A's credit released"
    assert await _is_escrowed(db_session, job_b) is True, "peer B's credit untouched"


# --------------------------------------------------------------------------- #
# record_canary_result: reputation + threshold release
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_canary_passes_build_reputation_then_release(
    db_session: AsyncSession,
) -> None:
    peer = await _peer(db_session)
    hb = PeerHeartbeat(
        peer_id=peer.id,
        last_beat_at=datetime.now(timezone.utc),
        binary_hash="h",
        client_version="v",
    )
    db_session.add(hb)
    await db_session.flush()
    job_id = await _escrowed_job_for_peer(db_session, peer.id)
    assert hb.reputation == pytest.approx(0.5)

    # First pass: 0.5 -> 0.6, still below threshold -> not released.
    released = await record_canary_result(db_session, peer.id, passed=True)
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.6)
    assert released == 0
    assert await _is_escrowed(db_session, job_id) is True

    # Second pass: 0.6 -> 0.7 >= threshold -> release.
    released = await record_canary_result(db_session, peer.id, passed=True)
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(RELEASE_REPUTATION_THRESHOLD)
    assert released == 1
    assert await _is_escrowed(db_session, job_id) is False


@pytest.mark.asyncio
async def test_canary_fail_lowers_reputation(db_session: AsyncSession) -> None:
    peer = await _peer(db_session)
    hb = PeerHeartbeat(
        peer_id=peer.id,
        last_beat_at=datetime.now(timezone.utc),
        binary_hash="h",
        client_version="v",
    )
    db_session.add(hb)
    await db_session.flush()

    released = await record_canary_result(db_session, peer.id, passed=False)
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.4)
    assert released == 0


# --------------------------------------------------------------------------- #
# run_canary_probe end-to-end with a simulated peer
# --------------------------------------------------------------------------- #


class _HonestPeerWS:
    """Fake peer WS that answers whatever canary it's sent, correctly."""

    async def send_json(self, payload: dict) -> None:
        from coordinator.services.peer_registry import complete_job, get_job

        job_id = uuid.UUID(payload["job_id"])
        prompt = payload["prompt"]
        expected = next((c.expected for c in CANARIES if c.prompt == prompt), "")
        buf = await get_job(job_id)
        if buf is not None:
            buf.tokens.append(expected)
            buf.error_category = None
            await complete_job(job_id)  # sets buf.done


@pytest.mark.asyncio
async def test_run_canary_probe_honest_peer_passes(db_session: AsyncSession) -> None:
    from coordinator.services.peer_registry import (
        PeerConnection,
        register_peer,
        unregister_peer,
    )

    peer = await _peer(db_session)
    db_session.add(
        PeerCapability(peer_id=peer.id, model_slug=_MODEL, quantization="q4_K_M")
    )
    hb = PeerHeartbeat(
        peer_id=peer.id,
        last_beat_at=datetime.now(timezone.utc),
        binary_hash="h",
        client_version="v",
    )
    db_session.add(hb)
    await db_session.flush()

    conn = PeerConnection(
        peer_id=peer.id,
        session_id=uuid.uuid4(),
        ws=_HonestPeerWS(),
        member_id=peer.member_id,
    )
    await register_peer(conn)
    try:
        passed = await run_canary_probe(db_session, peer.id)
    finally:
        await unregister_peer(peer.id)

    assert passed is True
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.6), "an honest probe raises reputation"


@pytest.mark.asyncio
async def test_run_canary_probe_offline_peer_returns_none(
    db_session: AsyncSession,
) -> None:
    # Peer exists in the DB but is not in the in-memory connection registry.
    peer = await _peer(db_session)
    assert await run_canary_probe(db_session, peer.id) is None


async def _online_honest_peer(db: AsyncSession) -> Peer:
    """A peer registered online with an honest canary-answering WS, a model
    capability, and a heartbeat (reputation 0.5)."""
    from coordinator.services.peer_registry import PeerConnection, register_peer

    peer = await _peer(db)
    db.add(PeerCapability(peer_id=peer.id, model_slug=_MODEL, quantization="q4_K_M"))
    db.add(
        PeerHeartbeat(
            peer_id=peer.id,
            last_beat_at=datetime.now(timezone.utc),
            binary_hash="h",
            client_version="v",
        )
    )
    await db.flush()
    await register_peer(
        PeerConnection(
            peer_id=peer.id,
            session_id=uuid.uuid4(),
            ws=_HonestPeerWS(),
            member_id=peer.member_id,
        )
    )
    return peer


@pytest.mark.asyncio
async def test_probe_due_peers_targets_escrowed_and_releases(
    db_session: AsyncSession,
) -> None:
    from coordinator.services.peer_registry import unregister_peer

    # peer_escrow holds escrowed credits; peer_clean has none.
    peer_escrow = await _online_honest_peer(db_session)
    peer_clean = await _online_honest_peer(db_session)
    job_id = await _escrowed_job_for_peer(db_session, peer_escrow.id)
    online = [peer_escrow.id, peer_clean.id]

    async def _rep(peer_id: uuid.UUID) -> float:
        hb = await db_session.get(PeerHeartbeat, peer_id)
        await db_session.refresh(hb)
        return hb.reputation

    try:
        # Cycle 1: only the escrowed peer is due. 0.5 -> 0.6, not yet released.
        probed = await probe_due_peers(db_session, online)
        assert probed == 1, "only the peer with escrowed credits should be probed"
        assert await _rep(peer_escrow.id) == pytest.approx(0.6)
        assert await _rep(peer_clean.id) == pytest.approx(0.5), "clean peer untouched"
        assert await _is_escrowed(db_session, job_id) is True

        # Cycle 2: 0.6 -> 0.7 >= threshold -> escrow released.
        probed = await probe_due_peers(db_session, online)
        assert probed == 1
        assert await _is_escrowed(db_session, job_id) is False, "escrow released"
    finally:
        await unregister_peer(peer_escrow.id)
        await unregister_peer(peer_clean.id)
