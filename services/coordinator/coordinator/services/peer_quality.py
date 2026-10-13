"""Peer quality scoring service.

Records quality observations and aggregates them into a dispatch weight
for the peer selection path in peer_registry.

Score formula (sliding window, recency-weighted mean):
------------------------------------------------------
Only observations within the last QUALITY_WINDOW_DAYS (7) days are used.
Each observation i is weighted by:

    w_i = sample_size_i * 2 ** (-age_hours_i / QUALITY_HALF_LIFE_HOURS)

where age_hours_i is the age of the observation in hours at query time and
QUALITY_HALF_LIFE_HOURS = 24.  An observation that is exactly 24 h old has
half the weight of a fresh one; at 4 days it has 1/16 the weight.

The composite quality score is:

    quality_score = sum(w_i * score_i) / sum(w_i)

Neutral default (no observations in window): 1.0. New peers are not penalised;
the open-pool reputation gate already protects strangers from unverified peers.

Consecutive failure exclusion policy:
--------------------------------------
An observation is a *failure* when score < QUALITY_FAIL_THRESHOLD (0.3).
consecutive_failures() counts trailing observations (most-recent-first),
stopping at the first observation >= QUALITY_FAIL_THRESHOLD.

When consecutive_failures() >= QUALITY_CONSECUTIVE_FAIL_THRESHOLD (default 3),
the peer is hard-excluded from all dispatch eligibility until a passing
observation is recorded.  A single bad signal never triggers exclusion
(the threshold is 3 by design, not 1).

Privacy stance:
---------------
PeerQualityScore rows contain only peer_id + model_slug + signal_type +
scalar score + sample_size + timestamp.  No prompt, no completion, no
requester identity.  The feedback endpoint (POST /jobs/{job_id}/feedback)
records only the binary rating and the job's peer/model - never prompt or
completion content.

Follow-ons (out of scope for this implementation):
  - Periodic probe scheduler (determinism/coherence shadow evals).
  - Commit-reveal adversarial probes.
  - Frontend quality badges / thumbs-down UI.
  - Bench-prompt ground truth corpus.
"""
from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal

# ---------------------------------------------------------------------------
# Configuration constants - all tunable; kept in one place for visibility.
# ---------------------------------------------------------------------------

#: Only observations within this many days contribute to the score.
QUALITY_WINDOW_DAYS: int = 7

#: Recency half-life.  An observation this many hours old has half the weight
#: of a fresh observation.  Controls how quickly the score responds to change.
QUALITY_HALF_LIFE_HOURS: float = 24.0

#: score < QUALITY_FAIL_THRESHOLD counts as a failure observation.
QUALITY_FAIL_THRESHOLD: float = 0.3

#: A peer is hard-excluded from dispatch when this many consecutive trailing
#: observations are each below QUALITY_FAIL_THRESHOLD.  Never triggered by a
#: single bad signal; raise to be more permissive.
QUALITY_CONSECUTIVE_FAIL_THRESHOLD: int = 3

#: Score returned when no observations exist in the current window.
QUALITY_NEUTRAL: float = 1.0

SignalType = Literal["determinism", "coherence", "latency", "user_feedback"]


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def record_quality_signal(
    db,  # AsyncSession - typed loosely to avoid sqlalchemy at module load
    peer_id: uuid.UUID,
    model_slug: str,
    signal_type: SignalType,
    score: float,
    sample_size: int = 1,
) -> None:
    """Append one quality observation for a (peer_id, model_slug) pair.

    score is clamped to [0.0, 1.0] at write time.  signal_type must be one
    of 'determinism', 'coherence', 'latency', or 'user_feedback'.

    The caller is responsible for committing the session after this call (or
    the write happens implicitly when the enclosing transaction commits).  This
    matches the credit-ledger pattern: functions add rows, endpoints commit.
    """
    from coordinator.models.peer_quality import PeerQualityScore

    row = PeerQualityScore(
        peer_id=peer_id,
        model_slug=model_slug,
        signal_type=signal_type,
        score=float(max(0.0, min(1.0, score))),
        sample_size=max(1, int(sample_size)),
    )
    db.add(row)


# ---------------------------------------------------------------------------
# Read / aggregate path - single-peer helpers
# ---------------------------------------------------------------------------


async def peer_quality_score(
    db,  # AsyncSession
    peer_id: uuid.UUID,
    model_slug: str,
) -> float:
    """Return the current quality score (0..1) for one (peer_id, model_slug).

    Returns QUALITY_NEUTRAL (1.0) when no observations fall within the window
    (new peer - do not penalise).

    See batch_quality_scores for the exact formula and window definition.
    """
    result = await batch_quality_scores(db, [peer_id], model_slug)
    return result.get(peer_id, QUALITY_NEUTRAL)


async def consecutive_failures(
    db,  # AsyncSession
    peer_id: uuid.UUID,
    model_slug: str,
) -> int:
    """Count trailing failure observations (most-recent-first) for one peer.

    Counting stops at the first observation where score >= QUALITY_FAIL_THRESHOLD.
    Returns 0 when there are no observations or the most recent observation is
    passing.
    """
    counts = await _fetch_trailing_failure_counts(db, [peer_id], model_slug)
    return counts.get(peer_id, 0)


# ---------------------------------------------------------------------------
# Read / aggregate path - batch helpers (used by peer_registry._score_peers
# and pick_peer_for_model for efficient per-dispatch lookups)
# ---------------------------------------------------------------------------


async def batch_quality_scores(
    db,  # AsyncSession
    peer_ids: list[uuid.UUID],
    model_slug: str,
) -> dict[uuid.UUID, float]:
    """Batch-compute quality scores for a list of peers.  One DB query.

    Returns {peer_id: score}; peers with no observations in the window are
    absent from the dict - the caller should default to QUALITY_NEUTRAL (1.0).

    Formula:
        weighted_score = sum(w_i * score_i) / sum(w_i)
        where w_i = sample_size_i * 2^(-age_hours_i / QUALITY_HALF_LIFE_HOURS)
    """
    if not peer_ids:
        return {}

    from sqlalchemy import select

    from coordinator.models.peer_quality import PeerQualityScore

    window_start = datetime.now(timezone.utc) - timedelta(days=QUALITY_WINDOW_DAYS)

    rows = (
        await db.execute(
            select(
                PeerQualityScore.peer_id,
                PeerQualityScore.score,
                PeerQualityScore.sample_size,
                PeerQualityScore.observed_at,
            ).where(
                PeerQualityScore.peer_id.in_(peer_ids),
                PeerQualityScore.model_slug == model_slug,
                PeerQualityScore.observed_at >= window_start,
            )
        )
    ).all()

    now = datetime.now(timezone.utc)
    # peer_id -> (weighted_score_sum, weight_sum)
    accum: dict[uuid.UUID, tuple[float, float]] = {}

    for pid, score, sample_size, observed_at in rows:
        # Ensure timezone-aware for age arithmetic.
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - observed_at).total_seconds() / 3600.0)
        w = float(sample_size) * math.pow(2.0, -age_hours / QUALITY_HALF_LIFE_HOURS)

        if pid not in accum:
            accum[pid] = (0.0, 0.0)
        ws, wt = accum[pid]
        accum[pid] = (ws + w * float(score), wt + w)

    return {
        pid: (ws / wt) if wt > 0.0 else QUALITY_NEUTRAL
        for pid, (ws, wt) in accum.items()
    }


async def batch_consecutive_fail_excluded(
    db,  # AsyncSession
    peer_ids: list[uuid.UUID],
    model_slug: str,
) -> set[uuid.UUID]:
    """Return peer_ids that have >= QUALITY_CONSECUTIVE_FAIL_THRESHOLD
    consecutive trailing failures and must be hard-excluded from dispatch.

    Uses a single window-function query to fetch the last
    QUALITY_CONSECUTIVE_FAIL_THRESHOLD rows per peer, then evaluates in
    Python.  Efficient for the typical small candidate set (< 20 peers).
    """
    if not peer_ids:
        return set()

    counts = await _fetch_trailing_failure_counts(db, peer_ids, model_slug)
    return {
        pid
        for pid, c in counts.items()
        if c >= QUALITY_CONSECUTIVE_FAIL_THRESHOLD
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_trailing_failure_counts(
    db,  # AsyncSession
    peer_ids: list[uuid.UUID],
    model_slug: str,
) -> dict[uuid.UUID, int]:
    """Return trailing consecutive failure counts per peer.

    Fetches the last QUALITY_CONSECUTIVE_FAIL_THRESHOLD observations per peer
    using a window function (one round-trip for the whole candidate set), then
    counts trailing failures in Python.  rn=1 is the most recent observation.
    """
    if not peer_ids:
        return {}

    from sqlalchemy import func, select

    from coordinator.models.peer_quality import PeerQualityScore

    # Subquery: rank observations per peer - most recent gets rn=1.
    ranked_subq = (
        select(
            PeerQualityScore.peer_id,
            PeerQualityScore.score,
            func.row_number()
            .over(
                partition_by=PeerQualityScore.peer_id,
                order_by=PeerQualityScore.observed_at.desc(),
            )
            .label("rn"),
        )
        .where(
            PeerQualityScore.peer_id.in_(peer_ids),
            PeerQualityScore.model_slug == model_slug,
        )
        .subquery("ranked_quality")
    )

    # Keep only the last N rows per peer (N = threshold).
    rows = (
        await db.execute(
            select(ranked_subq.c.peer_id, ranked_subq.c.score, ranked_subq.c.rn)
            .where(ranked_subq.c.rn <= QUALITY_CONSECUTIVE_FAIL_THRESHOLD)
            .order_by(ranked_subq.c.peer_id, ranked_subq.c.rn)
        )
    ).all()

    # Group by peer; rows are already in rn order (1=most recent, 2, 3...).
    obs_by_peer: dict[uuid.UUID, list[float]] = defaultdict(list)
    for pid, score, _rn in rows:
        obs_by_peer[pid].append(float(score))

    counts: dict[uuid.UUID, int] = {}
    for pid, scores in obs_by_peer.items():
        fail_count = 0
        for s in scores:  # most-recent-first (rn=1 is appended first)
            if s < QUALITY_FAIL_THRESHOLD:
                fail_count += 1
            else:
                break  # consecutive run broken - stop counting
        counts[pid] = fail_count

    return counts
