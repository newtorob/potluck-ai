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
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from coordinator.schemas.peer import JobAssignedMessage, SamplingParams
from coordinator.services.credit_ledger import (
    NEUTRAL_REPUTATION,
    RELEASE_REPUTATION_THRESHOLD,
    nudge_reputation,
    release_escrow_for_peer,
)

# Seconds to wait for a canary response before failing the probe.
CANARY_TIMEOUT_S = 30.0
# Probe sampling is randomized per call (see _probe_sampling) so a peer cannot
# fingerprint a canary by a constant seed / token budget and then answer probes
# honestly while junking real jobs. temp stays 0 so grading is deterministic
# (which also makes the seed cosmetic to the output, so randomizing it is free).
# The token budget jitters within a range whose floor leaves headroom for the
# answer PLUS a short preamble a chatty small model adds ("Sure, the code is
# ABCDEF"): too tight and an honest peer's correct answer is truncated and graded
# WRONG, which hard-resets its reputation.
# Remaining tells -- chiefly temp 0 (real jobs default to a higher temperature),
# plus the probe-shaped PROMPT a peer could still classify -- need canaries drawn
# from a realistic prompt corpus, a deeper follow-up. temp 0 is the strongest
# residual tell; randomizing the seed/budget does not touch it.
CANARY_MIN_TOKENS = 32  # truncation-safe floor (answer + a short preamble)
# Keep the ceiling modest: evaluate_canary matches the answer ANYWHERE in the
# output, so a wide budget gives a rambling/junk peer more room to incidentally
# emit a short numeric answer and false-PASS. 48 leaves preamble headroom while
# staying tight (nonce answers are immune; arithmetic answers are the exposed case).
CANARY_MAX_TOKENS = 48

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
    # The exact answer string the peer's output must contain (matched
    # case-insensitively on a token boundary; see evaluate_canary).
    expected: str


def evaluate_canary(output: str, canary: Canary) -> bool:
    """True if the peer's output contains the canary's expected answer.

    Case-insensitive, but the expected answer must appear on a TOKEN BOUNDARY
    (no [0-9a-z] immediately adjacent), not merely as a substring. That keeps the
    tolerance for an answer wrapped in punctuation/whitespace ("the answer is
    85.") while rejecting a wrong answer that happens to embed the expected one
    (a peer replying "185" must not satisfy an expected "85"). A junk-returner
    won't hit a distinctive token by chance.
    """
    expected = canary.expected.strip().lower()
    if not expected:
        return False
    haystack = output.strip().lower()
    return re.search(rf"(?<![0-9a-z]){re.escape(expected)}(?![0-9a-z])", haystack) is not None


# Canaries are GENERATED, not drawn from a fixed list: a small fixed set can be
# memorized (a fabricator learns the handful of answers, then passes every probe
# while junking real work). Every variant below has an exact, computed answer
# that any competent instruct model returns at temp 0 (no model-class-specific
# difficulty), so honest peers -- including the small models Potluck runs -- never
# false-fail, while a dead/junk peer fails.
_NONCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # uppercase, omit I/O for legibility
_NONCE_LEN = 6  # 24**6 ~= 191M distinct codes -> nothing to memorize


def _nonce_canary(rng: random.Random) -> Canary:
    """Instruction-following probe: echo a random code. Unmemorizable, model-
    agnostic, deterministic at temp 0."""
    nonce = "".join(rng.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LEN))
    return Canary(f"Reply with exactly this code and nothing else: {nonce}", nonce)


def _add_canary(a: int, b: int) -> Canary:
    return Canary(f"What is {a} plus {b}? Reply with only the number.", str(a + b))


def _sub_canary(a: int, b: int) -> Canary:
    return Canary(f"What is {a} minus {b}? Reply with only the number.", str(a - b))


def _mul_canary(a: int, b: int) -> Canary:
    return Canary(f"What is {a} times {b}? Reply with only the number.", str(a * b))


def generate_canary(rng: random.Random | None = None) -> Canary:
    """Produce a fresh canary from a large generated space (no fixed set to
    memorize). `rng` is injectable for deterministic tests; a fresh entropy-
    seeded Random is used otherwise.

    Scope: these catch a dead / junk / garbage-returning peer and cannot be
    memorized (an unbounded generated space, vs the old fixed four). What they do
    NOT catch -- same as the old fixed set -- is a peer that answers with a cheap
    script instead of the advertised model (a calculator + echo passes both), or
    one serving a smaller model than advertised. That is the per-model-class /
    downgrade-spoofing follow-up, which needs per-model calibration to avoid
    false-failing honest small-model peers, so the canaries here stay
    deliberately model-agnostic."""
    r = rng if rng is not None else random.Random()
    # Two bounds on arithmetic answers, both because evaluate_canary boundary-
    # matches the bare digits ANYWHERE in the output:
    #   - keep results < 1000, else a thousands separator ("1,085") false-fails;
    #   - keep results >= 2 digits, else a single digit ("9") is easily emitted
    #     incidentally by a rambling junk peer and would false-PASS.
    # The operand ranges below satisfy both: add 22..198, sub 10..88, mul 16..144.
    # Nonce echoes are weighted higher still: a 24**6 random code can't be matched
    # incidentally at all, so nonce probes give the most reliable WRONG grade.
    kind = r.choice(("nonce", "nonce", "nonce", "add", "sub", "mul"))
    if kind == "nonce":
        return _nonce_canary(r)
    if kind == "add":
        return _add_canary(r.randint(11, 99), r.randint(11, 99))  # 22..198
    if kind == "sub":
        return _sub_canary(r.randint(50, 99), r.randint(11, 40))  # 10..88, a > b
    return _mul_canary(r.randint(4, 12), r.randint(4, 12))  # 16..144


def _probe_sampling(rng: random.Random) -> SamplingParams:
    """Randomized per-probe sampling so a peer can't fingerprint a canary by a
    constant seed / token budget. temp stays 0 (deterministic grading); only the
    seed and the token budget vary, the latter within a truncation-safe range."""
    return SamplingParams(
        temperature=0.0,
        max_tokens=rng.randint(CANARY_MIN_TOKENS, CANARY_MAX_TOKENS),
        seed=rng.randint(0, 2**31 - 1),
    )


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

    rng = random.Random()
    canary = generate_canary(rng)
    job_id = uuid.uuid4()
    buf = await register_job(job_id)
    assigned = JobAssignedMessage(
        job_id=job_id,
        model_slug=model_slug,
        prompt=canary.prompt,
        sampling=_probe_sampling(rng),
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
# Floor re-probe: every online peer is probed with at least this probability per
# cycle, forever -- trust is never permanent, so a peer that earns trust then
# starts fabricating is still caught (without it, a peer past the release
# threshold with no escrow is never probed again). Lower-reputation peers are
# probed more (see _reprobe_probability); this only spends batch capacity left
# over after escrowed + recovering peers are served.
PROBE_FLOOR = 0.1


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


async def _peers_below_reentry(
    db: AsyncSession, online_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Online peers whose reputation has fallen below NEUTRAL — quarantined out
    of the open pool by the routing hysteresis (peer_registry). Probing them is
    the rehabilitation path: clean canaries climb a fallen peer back to neutral
    and out of quarantine. Without this a sub-neutral peer that holds no escrowed
    credits would never be probed (the escrowed set wouldn't include it) and
    could never earn its way back."""
    if not online_ids:
        return []
    from coordinator.models.peer import PeerHeartbeat

    rows = await db.execute(
        select(PeerHeartbeat.peer_id).where(
            PeerHeartbeat.peer_id.in_(online_ids),
            PeerHeartbeat.reputation < NEUTRAL_REPUTATION,
        )
    )
    return [r[0] for r in rows.all() if r[0] is not None]


async def _reputations(
    db: AsyncSession, peer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """Current reputation for each peer that has a heartbeat row. Peers absent
    from the result have no heartbeat yet (callers treat that as neutral)."""
    if not peer_ids:
        return {}
    from coordinator.models.peer import PeerHeartbeat

    rows = await db.execute(
        select(PeerHeartbeat.peer_id, PeerHeartbeat.reputation).where(
            PeerHeartbeat.peer_id.in_(peer_ids)
        )
    )
    return {pid: float(rep) for pid, rep in rows.all()}


def _dedup(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def select_due_peers(
    escrowed: list[uuid.UUID], recovering: list[uuid.UUID], batch: int
) -> list[uuid.UUID]:
    """Choose which peers to probe this cycle from the two due sets, capped at
    `batch`.

    Escrowed peers (credits waiting on a release) take priority, BUT we reserve up
    to batch//4 slots for recovering (sub-neutral) peers so a large escrowed
    cohort can't permanently starve rehabilitation — a starved quarantined peer
    could never earn its way back into the pool. A peer in both sets is probed
    once, via its escrowed slot (the recovering list is filtered against it)."""
    escrowed = _dedup(escrowed)
    esc_set = set(escrowed)
    recovering = [p for p in _dedup(recovering) if p not in esc_set]

    recover_quota = min(len(recovering), batch // 4)  # 0 when batch < 4
    escrow_take = escrowed[: batch - recover_quota]
    recover_take = recovering[: batch - len(escrow_take)]
    return escrow_take + recover_take


def _reprobe_probability(reputation: float | None) -> float:
    """Per-cycle probe probability for an online peer that is NOT otherwise due
    (no escrow, at or above neutral). Lower reputation -> probed more often;
    every peer floors at PROBE_FLOOR so trust is never permanent. A peer with no
    heartbeat row yet is treated as neutral."""
    rep = NEUTRAL_REPUTATION if reputation is None else reputation
    return max(PROBE_FLOOR, 1.0 - rep)


def _select_reprobe(
    candidates: list[tuple[uuid.UUID, float | None]],
    slots: int,
    rng: random.Random,
) -> list[uuid.UUID]:
    """From online peers not otherwise due, pick which to floor-re-probe this
    cycle: each is drawn independently with _reprobe_probability(rep). If more
    draw than `slots`, pick a RANDOM subset.

    Deliberately NOT lowest-reputation-first: the draw probability already weights
    suspicion (lower rep draws more), and a low-rep bias here would let a flood of
    cheap neutral decoys crowd out re-probing of the HIGH-reputation peers that
    floor-reprobe exists to recheck (a trusted-then-bad peer sits at high rep
    until it is caught). Random truncation keeps a high-rep peer's slot odds fair
    instead of always last."""
    if slots <= 0:
        return []
    drawn = [
        pid for pid, rep in candidates if rng.random() < _reprobe_probability(rep)
    ]
    if len(drawn) <= slots:
        return drawn
    return rng.sample(drawn, slots)


async def probe_due_peers(
    db: AsyncSession,
    online_ids: list[uuid.UUID],
    batch: int = PROBE_BATCH,
    rng: random.Random | None = None,
) -> int:
    """One verification cycle. Probes up to `batch` peers in priority order:

      1. DUE peers — holding escrowed credits (a clean canary moves them toward
         release) or quarantined below neutral (toward pool re-entry). See
         select_due_peers for the escrow-priority-with-rehab-reserve policy.
      2. FLOOR re-probe — any leftover batch capacity surprise-probes
         otherwise-not-due online peers so trust is never permanent (catch a peer
         that earned trust then went bad). Adaptive: lower-reputation peers are
         probed more, all flooring at PROBE_FLOOR (see _reprobe_probability).

    Returns the number probed. A single probe failure is logged and does not abort
    the cycle."""
    rng = rng or random.Random()
    online_ids = _dedup(online_ids)  # defensive: never probe a peer twice in a cycle
    escrowed = await _peers_with_escrowed_credits(db, online_ids)
    recovering = await _peers_below_reentry(db, online_ids)
    due = select_due_peers(escrowed, recovering, batch)

    spare = batch - len(due)
    if spare > 0:
        # Floor-reprobe candidates are every OTHER online peer, deliberately not
        # scoped to pool contributors: probing a My-Machines-only peer is harmless
        # (a working peer passes, and reputation never gates My-Machines routing),
        # so we skip the extra capability/scope join.
        excluded = set(escrowed) | set(recovering) | set(due)
        others = [pid for pid in online_ids if pid not in excluded]
        if others:
            reps = await _reputations(db, others)
            candidates = [(pid, reps.get(pid)) for pid in others]
            due = due + _select_reprobe(candidates, spare, rng)

    probed = 0
    for peer_id in due:
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
