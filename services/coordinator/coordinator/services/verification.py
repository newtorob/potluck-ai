"""Verification source for the credit-escrow primitive — canary probes.

Cross-member (public-pool) earned credits are escrowed until verified (see
services/credit_ledger.py). This module is the *source* that verifies a peer:
it sends the peer a coordinator-owned CANARY job — a known prompt with a known
answer, at temperature 0 — and checks the response.

Why canaries (not naive re-execution comparison): a simple instruction or
arithmetic canary is answered identically by any competent model regardless of
GPU / quantization, so a junk-returner or dead peer fails it while honest peers
pass. Re-execution comparison across heterogeneous consumer hardware false-fails
honest peers on sampling nondeterminism (flagged in the design doc); canaries
sidestep that, and the prompts are coordinator-owned so no user content is
captured.

A passed canary raises the peer's reputation; once reputation crosses
RELEASE_REPUTATION_THRESHOLD the peer's escrowed earned credits are released. A
failed canary lowers reputation.

The canary uses a fresh in-memory JobBuffer + job_id and never creates a DB Job
row or writes credits. It flows through the same peer message handler as real
jobs (so a peer can't cheaply tell a probe from real work without solving it),
but bypasses the credit/finalize path entirely.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from coordinator.schemas.peer import JobAssignedMessage, SamplingParams
from coordinator.services.credit_ledger import (
    RELEASE_REPUTATION_THRESHOLD,
    nudge_reputation,
    release_escrow_for_peer,
)

# Seconds to wait for a canary response before failing the probe.
CANARY_TIMEOUT_S = 30.0
# Pinned seed + tiny token budget — probes should be cheap and deterministic.
CANARY_SEED = 7
CANARY_MAX_TOKENS = 16

# Canary outcome grades. Reputation treats these very differently (see
# credit_ledger.nudge_reputation): only a clean wrong answer is an unambiguous
# fabrication signal (hard reset); a timeout / delivery failure / peer-reported
# error is an ambiguous miss (soft penalty), so we never nuke an honest-but-flaky
# peer for what may be the network. Values match credit_ledger._OUTCOME_*.
CANARY_PASS = "pass"
CANARY_WRONG = "wrong"
CANARY_INFRA = "infra"


@dataclass(frozen=True)
class Canary:
    prompt: str
    # Lowercased substring the correct answer must contain.
    expected: str


# Model-agnostic canaries: instructions / facts any competent model answers
# identically at temp 0. Short by design. Per-model-class canaries that catch
# model-spoofing specifically are a follow-up.
CANARIES: tuple[Canary, ...] = (
    Canary("What is 7 plus 5? Reply with only the number.", "12"),
    Canary("Reply with exactly this word and nothing else: POTLUCK", "potluck"),
    Canary("What is the capital of France? Reply with only the city name.", "paris"),
    Canary("Complete with one word: the opposite of 'hot' is", "cold"),
)


def evaluate_canary(output: str, canary: Canary) -> bool:
    """True if the peer's output contains the canary's expected answer.

    Case-insensitive substring match on stripped output — small models often
    wrap a correct answer in punctuation/whitespace, and the expected tokens are
    distinctive enough that a junk-returner won't hit them by chance.
    """
    return canary.expected.lower() in output.strip().lower()


def _pick_canary() -> Canary:
    return random.choice(CANARIES)


async def _peer_model_slug(db: AsyncSession, peer_id: uuid.UUID) -> str | None:
    """One model the peer advertises (any works for a model-agnostic canary)."""
    from coordinator.models import PeerCapability

    return (
        await db.execute(
            select(PeerCapability.model_slug)
            .where(PeerCapability.peer_id == peer_id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def record_canary_result(
    db: AsyncSession, peer_id: uuid.UUID, outcome: str
) -> int:
    """Apply a canary `outcome` (CANARY_PASS / CANARY_WRONG / CANARY_INFRA) to the
    peer's reputation, and release the peer's escrowed credits once reputation
    crosses RELEASE_REPUTATION_THRESHOLD.

    Only a clean PASS can trigger a release — a wrong/infra outcome lowers (or
    hard-resets) reputation and never releases. Returns the number of escrowed
    rows released (0 if none / threshold not yet reached).
    """
    from coordinator.models.peer import PeerHeartbeat

    hb = await db.get(PeerHeartbeat, peer_id)
    if hb is None:
        return 0
    hb.reputation = nudge_reputation(hb.reputation, outcome)
    await db.flush()

    if outcome == CANARY_PASS and hb.reputation >= RELEASE_REPUTATION_THRESHOLD:
        return await release_escrow_for_peer(db, peer_id)
    return 0


async def run_canary_probe(db: AsyncSession, peer_id: uuid.UUID) -> bool | None:
    """Send a canary to the given peer and record the result.

    Returns True (passed), False (wrong answer / error / timeout), or None if
    the probe couldn't run (peer offline, or advertises no model). The canary
    job is in-memory only — no DB Job row, no credit write.
    """
    from coordinator.services.peer_registry import (
        complete_job,
        get_peer,
        register_job,
    )

    peer = await get_peer(peer_id)
    if peer is None:
        return None
    model_slug = await _peer_model_slug(db, peer_id)
    if model_slug is None:
        return None

    canary = _pick_canary()
    job_id = uuid.uuid4()
    buf = await register_job(job_id)
    assigned = JobAssignedMessage(
        job_id=job_id,
        model_slug=model_slug,
        prompt=canary.prompt,
        sampling=SamplingParams(
            temperature=0.0, max_tokens=CANARY_MAX_TOKENS, seed=CANARY_SEED
        ),
    )

    try:
        await peer.ws.send_json(assigned.model_dump(mode="json"))
        await asyncio.wait_for(buf.done.wait(), timeout=CANARY_TIMEOUT_S)
        if buf.error_category is not None:
            # Peer reported it couldn't run the job (oom / declined / model not
            # loaded / internal). It failed to do the work, but that is NOT a
            # wrong-answer fabrication signal — grade it ambiguous.
            outcome = CANARY_INFRA
        elif evaluate_canary("".join(buf.tokens), canary):
            outcome = CANARY_PASS
        else:
            # Clean completion with the wrong answer — the unambiguous junk /
            # fabrication signal that hard-resets reputation.
            outcome = CANARY_WRONG
    except Exception:
        # Delivery failure or timeout: we never got a clean answer to grade, so
        # this is ambiguous (could be the network), not a fabrication.
        outcome = CANARY_INFRA
    finally:
        await complete_job(job_id)  # idempotent cleanup

    await record_canary_result(db, peer_id, outcome)
    return outcome == CANARY_PASS


# --------------------------------------------------------------------------- #
# Trigger — the background loop that decides WHEN to probe.
# --------------------------------------------------------------------------- #

# How often the loop wakes, and how many peers it probes per cycle (bounded so a
# burst of escrowed peers can't hammer the fleet in one tick).
PROBE_INTERVAL_S = 120.0
PROBE_BATCH = 8


async def _peers_with_escrowed_credits(
    db: AsyncSession, online_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Online peers that currently hold escrowed earned credits — the set worth
    probing (a passed canary releases their escrow once they cross the trust
    threshold). Peers with nothing escrowed are skipped."""
    if not online_ids:
        return []
    from coordinator.models.credit import CreditLedger
    from coordinator.models.job import Job

    rows = await db.execute(
        select(Job.peer_id)
        .distinct()
        .join(CreditLedger, CreditLedger.job_id == Job.id)
        .where(
            CreditLedger.direction == "earned",
            CreditLedger.escrowed.is_(True),
            Job.peer_id.in_(online_ids),
        )
    )
    return [r[0] for r in rows.all() if r[0] is not None]


async def probe_due_peers(
    db: AsyncSession, online_ids: list[uuid.UUID], batch: int = PROBE_BATCH
) -> int:
    """One verification cycle: probe up to `batch` online peers that hold
    escrowed credits. Returns the number probed. A single probe failure is
    logged and does not abort the cycle."""
    due = await _peers_with_escrowed_credits(db, online_ids)
    probed = 0
    for peer_id in due[:batch]:
        try:
            await run_canary_probe(db, peer_id)
            probed += 1
        except Exception:
            logger.exception("canary_probe_failed peer_id=%s", peer_id)
    return probed


async def run_verification_loop(
    stop: asyncio.Event, interval_s: float = PROBE_INTERVAL_S
) -> None:
    """Background loop wired into the coordinator lifespan. Every `interval_s`
    it probes online peers holding escrowed credits, releasing their credits as
    they prove themselves. One DB session + transaction per cycle; an iteration
    failure is logged and the loop keeps running. Stops promptly when `stop`
    is set."""
    from coordinator.db.database import get_session_factory
    from coordinator.services.peer_registry import online_peer_ids

    logger.info("verification_loop_started interval_s=%s", interval_s)
    while not stop.is_set():
        try:
            online_ids = await online_peer_ids()
            if online_ids:
                async with get_session_factory()() as db:
                    probed = await probe_due_peers(db, online_ids)
                    await db.commit()
                    if probed:
                        logger.info("verification_cycle_probed n=%d", probed)
        except Exception:
            logger.exception("verification_loop_iteration_failed")
        # Interruptible sleep so shutdown doesn't wait a full interval.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("verification_loop_stopped")
