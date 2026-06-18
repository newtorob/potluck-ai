"""Tests for the canary verification source + reputation-gated escrow release.

Requires a live Postgres via the db_session fixture (DB writes rolled back).
The in-memory peer/job registry is module-global, so tests that touch it clean
up after themselves.
"""
from __future__ import annotations

import random
import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger
from coordinator.models.job import Job
from coordinator.models.peer import Peer, PeerCapability, PeerHeartbeat
from coordinator.services.credit_ledger import (
    RELEASE_MIN_PASSES,
    RELEASE_REPUTATION_THRESHOLD,
    record_job_credits,
    release_escrow_for_peer,
)
from coordinator.services.verification import (
    CANARY_INFRA,
    CANARY_MAX_TOKENS,
    CANARY_MIN_TOKENS,
    CANARY_PASS,
    CANARY_WRONG,
    Canary,
    _add_canary,
    _mul_canary,
    _probe_sampling,
    _sub_canary,
    evaluate_canary,
    generate_canary,
    probe_due_peers,
    record_canary_result,
    run_canary_probe,
    select_due_peers,
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


def test_evaluate_canary_boundary_rejects_embedded_answer() -> None:
    # The expected answer must land on a token boundary, so a wrong answer that
    # merely embeds the expected one does NOT pass.
    c = Canary("What is 40 plus 45? Reply with only the number.", "85")
    assert evaluate_canary("85", c) is True
    assert evaluate_canary("The answer is 85.", c) is True  # wrapped still passes
    assert evaluate_canary("185", c) is False, "85 embedded in 185 must not pass"
    assert evaluate_canary("850", c) is False
    # Nonce / word answers match case-insensitively on a boundary too.
    n = Canary("Reply with exactly this code and nothing else: ZXQKMR", "ZXQKMR")
    assert evaluate_canary("zxqkmr", n) is True
    assert evaluate_canary("AZXQKMR", n) is False  # embedded -> reject


def test_generate_canary_is_varied_and_self_consistent() -> None:
    rng = random.Random(12345)
    canaries = [generate_canary(rng) for _ in range(200)]
    # No tiny fixed set to memorize: many distinct prompts.
    assert len({c.prompt for c in canaries}) > 100
    # Each canary's own expected answer satisfies it, and an honest solver
    # reproduces that answer from the prompt alone.
    for c in canaries:
        assert evaluate_canary(c.expected, c) is True
        assert _solve_canary_prompt(c.prompt) == c.expected


def test_generate_canary_arithmetic_is_correct() -> None:
    assert _add_canary(47, 38).expected == "85"
    assert _sub_canary(90, 33).expected == "57"
    assert _mul_canary(7, 8).expected == "56"
    # Subtraction operands are always a > b in generate_canary -> never negative.
    rng = random.Random(99)
    for _ in range(100):
        c = generate_canary(rng)
        assert not c.expected.startswith("-"), "no negative-answer canaries"


def test_probe_sampling_randomized_and_truncation_safe() -> None:
    # A peer must not be able to fingerprint a probe by a constant seed / budget.
    samples = [_probe_sampling(random.Random(s)) for s in range(50)]
    assert len({s.seed for s in samples}) > 1, "seed must vary across probes"
    assert len({s.max_tokens for s in samples}) > 1, "token budget must vary"
    for s in samples:
        assert s.temperature == 0.0, "temp 0 keeps grading deterministic"
        assert CANARY_MIN_TOKENS <= s.max_tokens <= CANARY_MAX_TOKENS
        assert s.seed is not None and 0 <= s.seed <= 2**31 - 1


def test_select_due_peers_policy() -> None:
    ids = [uuid.uuid4() for _ in range(20)]
    esc = ids[:12]   # 12 escrowed peers
    rec = ids[12:18]  # 6 recovering peers

    # Escrow priority, but a large escrowed cohort cannot starve recovery: with
    # batch 8, recovery is guaranteed batch//4 = 2 slots, escrow takes the rest.
    due = select_due_peers(esc, rec, batch=8)
    assert len(due) == 8
    assert due[:6] == esc[:6], "escrowed peers fill priority slots"
    assert set(due[6:]) == set(rec[:2]), "recovery keeps its reserved 2 slots"

    # No recovering peers -> escrow takes the whole batch.
    assert select_due_peers(esc, [], batch=8) == esc[:8]

    # No escrowed peers -> recovery takes the whole batch.
    assert select_due_peers([], rec, batch=8) == rec[:6]

    # A peer in BOTH sets is probed once (via its escrowed slot), not twice.
    dual = ids[0]
    due2 = select_due_peers([dual], [dual], batch=8)
    assert due2 == [dual]

    # Tiny batch (<4): no reserve, escrow keeps strict priority.
    assert select_due_peers(esc, rec, batch=1) == esc[:1]


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

    # A fresh peer (neutral 0.5 prior) must show RELEASE_MIN_PASSES clean canaries
    # before its escrow releases — the threshold is derived from the Sarmenta
    # credibility curve, not a fixed two-pass gate.
    for _ in range(1, RELEASE_MIN_PASSES):  # passes 1 .. k*-1: still below threshold
        released = await record_canary_result(db_session, peer.id, CANARY_PASS)
        assert released == 0, f"must not release before {RELEASE_MIN_PASSES} passes"
        assert await _is_escrowed(db_session, job_id) is True

    # The k*-th clean pass crosses the threshold and releases.
    released = await record_canary_result(db_session, peer.id, CANARY_PASS)
    await db_session.refresh(hb)
    assert hb.reputation >= RELEASE_REPUTATION_THRESHOLD
    assert released == 1
    assert await _is_escrowed(db_session, job_id) is False


@pytest.mark.asyncio
async def test_canary_infra_soft_penalty_wrong_hard_resets(
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

    # Ambiguous miss (timeout / delivery / peer-reported error): soft -STEP,
    # never a release. An honest-but-flaky peer must not be nuked.
    released = await record_canary_result(db_session, peer.id, CANARY_INFRA)
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.4)
    assert released == 0

    # Climb back up, then a confirmed WRONG answer hard-resets reputation to 0.
    await record_canary_result(db_session, peer.id, CANARY_PASS)  # 0.4 -> 0.5
    await record_canary_result(db_session, peer.id, CANARY_PASS)  # 0.5 -> 0.6
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.6)

    released = await record_canary_result(db_session, peer.id, CANARY_WRONG)
    await db_session.refresh(hb)
    assert hb.reputation == pytest.approx(0.0), "a wrong answer hard-resets reputation"
    assert released == 0


# --------------------------------------------------------------------------- #
# run_canary_probe end-to-end with a simulated peer
# --------------------------------------------------------------------------- #


def _solve_canary_prompt(prompt: str) -> str:
    """Compute the correct answer to a generated canary prompt (the honest-peer
    side of a probe). Mirrors generate_canary's templates."""
    p = prompt.strip()
    if p.startswith("Reply with exactly this code"):
        return p.rsplit(":", 1)[1].strip()
    m = re.search(r"What is (\d+) (plus|minus|times) (\d+)", p)
    assert m is not None, f"unrecognized canary prompt: {prompt!r}"
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    return str(a + b if op == "plus" else a - b if op == "minus" else a * b)


class _HonestPeerWS:
    """Fake peer WS that solves whatever canary it's sent, correctly."""

    async def send_json(self, payload: dict) -> None:
        from coordinator.services.peer_registry import complete_job, get_job

        job_id = uuid.UUID(payload["job_id"])
        buf = await get_job(job_id)
        if buf is not None:
            buf.tokens.append(_solve_canary_prompt(payload["prompt"]))
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

        # Cycles 2 .. k*-1: still climbing, still escrowed.
        for _ in range(2, RELEASE_MIN_PASSES):
            assert await probe_due_peers(db_session, online) == 1
            assert await _is_escrowed(db_session, job_id) is True

        # The k*-th honest cycle crosses the threshold -> escrow released.
        probed = await probe_due_peers(db_session, online)
        assert probed == 1
        assert await _is_escrowed(db_session, job_id) is False, "escrow released"
    finally:
        await unregister_peer(peer_escrow.id)
        await unregister_peer(peer_clean.id)


@pytest.mark.asyncio
async def test_probe_due_peers_rehabilitates_sub_neutral_peer(
    db_session: AsyncSession,
) -> None:
    # A peer quarantined below NEUTRAL (e.g. after a fabrication reset) that holds
    # NO escrowed credits must still be probed, so it has a path back into the
    # pool. Without _peers_below_reentry it would never be probed and could never
    # recover.
    from coordinator.services.peer_registry import unregister_peer

    fallen = await _online_honest_peer(db_session)
    hb = await db_session.get(PeerHeartbeat, fallen.id)
    hb.reputation = 0.2  # below NEUTRAL (0.5), no escrowed credits
    await db_session.flush()
    try:
        probed = await probe_due_peers(db_session, [fallen.id])
        assert probed == 1, "a sub-neutral peer must be probed even with no escrow"
        await db_session.refresh(hb)
        assert hb.reputation == pytest.approx(0.3), "a clean rehab probe climbs +STEP"
    finally:
        await unregister_peer(fallen.id)
