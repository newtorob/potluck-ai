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
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.schemas.peer import JobAssignedMessage, SamplingParams
from coordinator.services.credit_ledger import (
    RELEASE_REPUTATION_THRESHOLD,
    REPUTATION_STEP,
    release_escrow_for_peer,
)

# Seconds to wait for a canary response before failing the probe.
CANARY_TIMEOUT_S = 30.0
# Pinned seed + tiny token budget — probes should be cheap and deterministic.
CANARY_SEED = 7
CANARY_MAX_TOKENS = 16


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
    # Rotate via a fresh uuid's entropy (Math.random is unavailable here).
    return CANARIES[uuid.uuid4().int % len(CANARIES)]


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
    db: AsyncSession, peer_id: uuid.UUID, passed: bool
) -> int:
    """Apply a canary outcome to the peer's reputation, and release the peer's
    escrowed credits once reputation crosses RELEASE_REPUTATION_THRESHOLD.

    Returns the number of escrowed rows released (0 if none / threshold not yet
    reached).
    """
    from coordinator.models.peer import PeerHeartbeat

    hb = await db.get(PeerHeartbeat, peer_id)
    if hb is None:
        return 0
    if passed:
        hb.reputation = min(1.0, hb.reputation + REPUTATION_STEP)
    else:
        hb.reputation = max(0.0, hb.reputation - REPUTATION_STEP)
    await db.flush()

    if hb.reputation >= RELEASE_REPUTATION_THRESHOLD:
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
        passed = buf.error_category is None and evaluate_canary(
            "".join(buf.tokens), canary
        )
    except Exception:
        # Any failure to deliver or complete the probe counts as a fail — a
        # peer that can't answer a canary doesn't get its credits released.
        passed = False
    finally:
        await complete_job(job_id)  # idempotent cleanup

    await record_canary_result(db, peer_id, passed)
    return passed
