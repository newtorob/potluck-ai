"""Credit-ledger write path.

`record_job_credits` is the single function any inference-finalizer
calls when a job completes successfully. It writes two rows (one
`earned` for the contributor's owner, one `spent` for the requester),
both snapshotting the model-class multiplier at write time, and both
chained via SHA-256 to the previous row for that member.

Asymmetric earn/spend:

    spent_credits = output_tokens × multiplier
    earned_credits = output_tokens × multiplier × CONTRIBUTION_BONUS_MULTIPLIER

`CONTRIBUTION_BONUS_MULTIPLIER` is a module constant for v0 (default
1.5). When governance can vote, this moves to a config row.

Hash chain is per-member: each new row's `prev_row_hash` is the
previous row's `row_hash` for the *same* member. Bootstrap value for
the first row in a chain is the constant `GENESIS_HASH`.

Idempotency (migration 0003):
`credit_ledger` carries UNIQUE (job_id, direction). `record_job_credits`
uses INSERT … ON CONFLICT (job_id, direction) DO NOTHING. Calling this
function twice for the same job is a safe no-op - the second call
returns the rows that were created by the first call, identical to what
the first call returned.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger, CreditModelMultiplier

# Asymmetric earn/spend multiplier. The 1.5x reward for serving (vs.
# consuming) is what makes "run a peer 24/7" the dominant strategy on
# net balance, as in a torrent share ratio.
CONTRIBUTION_BONUS_MULTIPLIER = Decimal("1.5")

# Fallback multiplier when a job ran on a model_slug we don't have in
# `credit_model_multipliers`. Conservative - we'd rather under-credit
# than over-credit an unknown model. A warning is logged + the slug
# should be added to the table.
UNKNOWN_MODEL_MULTIPLIER = Decimal("1.0")

# Constant used for the very first row in a member's chain.
GENESIS_HASH = hashlib.sha256(b"potluck-credit-ledger-genesis").hexdigest()

# --- Reputation / escrow-release policy (Sarmenta spot-checking math) -------- #
#
# A peer's reputation is a bounded streak score in [0, 1]: it starts at the
# NEUTRAL prior, gains REPUTATION_STEP per clean canary, takes a soft
# REPUTATION_STEP penalty on an ambiguous miss (timeout / delivery / peer-side
# error), and hard-resets to 0 on a confirmed wrong answer. Because a confirmed
# fabrication zeroes it, reputation reflects an unbroken run of clean passes.
#
# The release threshold is derived from a target accepted-error budget, not
# picked ad hoc. Sarmenta's worker credibility after k clean spot-checks is
#       Cr(k) = 1 - (f/(1-f)) * 1/(k*e)
# with f = the assumed fraction of bad peers. The Credibility-Threshold
# Principle says: accept results only when Cr >= theta and the accepted-error
# rate is bounded by <= 1 - theta. So for a target accepted-error eps we set
# theta = 1 - eps and require enough clean passes to clear it:
#       k* = smallest k with Cr(k) >= 1 - eps  =  ceil( f / ((1-f) * eps * e) )
# f = 0.10, eps = 0.01  ->  k* = ceil(0.1 / (0.9 * 0.01 * e)) = ceil(4.09) = 5.
#
# We then size the streak scale so a *fresh* peer must show k* clean canaries
# before its escrow releases. (A peer caught fabricating resets to 0 and faces a
# steeper climb than a newcomer - intended: trust is cheap to lose, costly to
# regain.) Reputation here is a streak accumulator, not a literal credibility
# value - the curve is used only to choose how many clean passes we demand.
REPUTATION_STEP = 0.1
NEUTRAL_REPUTATION = 0.5  # fresh-peer prior; must match PeerHeartbeat.reputation default

ASSUMED_BAD_PEER_FRACTION = 0.10  # f
TARGET_ACCEPTED_ERROR = 0.01      # eps  -> release credibility target 1 - eps = 0.99
RELEASE_MIN_PASSES = math.ceil(
    ASSUMED_BAD_PEER_FRACTION
    / ((1 - ASSUMED_BAD_PEER_FRACTION) * TARGET_ACCEPTED_ERROR * math.e)
)  # = 5

# Gate half a step into the final pass so float rounding on repeated +STEP can't
# strand a peer that has met RELEASE_MIN_PASSES just below the line.
# = 0.5 + (5 - 0.5)*0.1 = 0.95  (4 clean passes = 0.9 < 0.95 <= 5 passes = ~1.0)
RELEASE_REPUTATION_THRESHOLD = NEUTRAL_REPUTATION + (RELEASE_MIN_PASSES - 0.5) * REPUTATION_STEP

# Canary outcome grades (plain strings to avoid a verification->credit_ledger
# import cycle; verification.py re-exports named constants for its own use).
_OUTCOME_PASS = "pass"
_OUTCOME_WRONG = "wrong"
_OUTCOME_INFRA = "infra"


def nudge_reputation(current: float, outcome: str) -> float:
    """Update a reputation streak score (0..1) by canary `outcome`:

      "pass"  -> +REPUTATION_STEP toward 1.0 (one more clean canary in the streak)
      "infra" -> -REPUTATION_STEP toward 0.0 (ambiguous miss: timeout / delivery
                 failure / peer-reported error - penalize lightly; do not nuke an
                 honest-but-flaky peer for something that may be the network)
      "wrong" -> hard reset to 0.0 (a clean completion with the wrong answer is an
                 unambiguous junk/fabrication signal; Sarmenta backtracking +
                 BOINC adaptive-replication CV-reset-to-0)

    Clamped to [0, 1]. Any unrecognized outcome is treated as the conservative
    "infra" soft penalty."""
    if outcome == _OUTCOME_PASS:
        return min(1.0, current + REPUTATION_STEP)
    if outcome == _OUTCOME_WRONG:
        return 0.0
    return max(0.0, current - REPUTATION_STEP)


def _compute_row_hash(
    member_id: uuid.UUID,
    direction: str,
    job_id: uuid.UUID,
    model_slug: str,
    output_tokens: int,
    multiplier: Decimal,
    credits: Decimal,
    prev_row_hash: str,
) -> str:
    """Stable SHA-256 over the canonical row contents + previous hash.

    Stability requires ordered-field serialization - never use a Python
    dict here, as the iteration order would silently shift across
    refactors and break the chain on existing rows."""
    canonical = "|".join(
        [
            str(member_id),
            direction,
            str(job_id),
            model_slug,
            str(output_tokens),
            f"{multiplier:.2f}",
            f"{credits:.2f}",
            prev_row_hash,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _latest_row_hash_for_member(
    db: AsyncSession, member_id: uuid.UUID
) -> str:
    """Look up the most recent row's row_hash for this member, or
    `GENESIS_HASH` if they have no rows yet."""
    result = await db.execute(
        select(CreditLedger.row_hash)
        .where(CreditLedger.member_id == member_id)
        .order_by(CreditLedger.created_at.desc(), CreditLedger.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row if row is not None else GENESIS_HASH


async def _resolve_multiplier(
    db: AsyncSession, model_slug: str
) -> Decimal:
    """Look up the multiplier for this model slug. Returns the configured
    value or `UNKNOWN_MODEL_MULTIPLIER` as a conservative default."""
    result = await db.execute(
        select(CreditModelMultiplier.multiplier).where(
            CreditModelMultiplier.slug == model_slug
        )
    )
    found = result.scalar_one_or_none()
    if found is None:
        return UNKNOWN_MODEL_MULTIPLIER
    return Decimal(found)


async def _fetch_existing_rows(
    db: AsyncSession, job_id: uuid.UUID
) -> tuple[CreditLedger, CreditLedger] | None:
    """Return the (spent, earned) rows for job_id if both exist, else None."""
    result = await db.execute(
        select(CreditLedger)
        .where(CreditLedger.job_id == job_id)
        .order_by(CreditLedger.created_at.asc(), CreditLedger.id.asc())
    )
    rows = result.scalars().all()
    if len(rows) < 2:
        return None
    by_direction = {r.direction: r for r in rows}
    if "spent" not in by_direction or "earned" not in by_direction:
        return None
    return by_direction["spent"], by_direction["earned"]


async def record_job_credits(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    requester_member_id: uuid.UUID,
    contributor_owner_member_id: uuid.UUID,
    model_slug: str,
    output_tokens: int,
    contributor_peer_id: uuid.UUID | None = None,
) -> tuple[CreditLedger, CreditLedger]:
    """Write the two ledger rows for a completed job. Returns the
    (spent_row, earned_row) tuple.

    Idempotent: calling this function twice for the same job_id is safe.
    The second call detects the conflict via UNIQUE (job_id, direction),
    skips the insert, and returns the rows created by the first call.
    The hash chain is unaffected - the no-op insert does not write a
    new row, so the chain state is identical to what the first call
    produced.
    """
    if output_tokens <= 0:
        # No credit for zero-output jobs. Callers shouldn't invoke us
        # in that case; we accept and silently no-op to be defensive.
        raise ValueError(
            f"refusing to credit job {job_id} with output_tokens={output_tokens}"
        )

    # --- Serialize per-member chain writes with Postgres advisory locks ------- #
    #
    # Two concurrent _finalize_job_in_db tasks for the same member both read
    # _latest_row_hash_for_member before either INSERT commits, then both insert
    # rows with the same prev_row_hash - forking the hash chain. The (job_id,
    # direction) UNIQUE constraint catches duplicate jobs but not two different
    # jobs racing on the same member's chain tip.
    #
    # pg_advisory_xact_lock is transaction-scoped: the lock is held until the
    # caller's transaction commits or rolls back (i.e. when the outer `async with
    # get_session_factory()() as db` block exits). Two tasks for the same member
    # will serialize here - the second blocks until the first transaction
    # completes, at which point it reads the now-correct chain tip.
    #
    # Acquire in sorted order to avoid deadlock when requester != contributor.
    members_to_lock = sorted(
        {requester_member_id, contributor_owner_member_id},
        key=lambda m: m.int,
    )
    for _mid in members_to_lock:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:mid))"),
            {"mid": str(_mid)},
        )

    multiplier = await _resolve_multiplier(db, model_slug)
    spent = Decimal(output_tokens) * multiplier
    earned = spent * CONTRIBUTION_BONUS_MULTIPLIER

    # --- 'spent' row for the requester --- #
    spent_prev = await _latest_row_hash_for_member(db, requester_member_id)
    spent_hash = _compute_row_hash(
        member_id=requester_member_id,
        direction="spent",
        job_id=job_id,
        model_slug=model_slug,
        output_tokens=output_tokens,
        multiplier=multiplier,
        credits=spent,
        prev_row_hash=spent_prev,
    )

    spent_stmt = (
        pg_insert(CreditLedger)
        .values(
            member_id=requester_member_id,
            direction="spent",
            job_id=job_id,
            model_slug=model_slug,
            output_tokens=output_tokens,
            multiplier=multiplier,
            credits=spent,
            prev_row_hash=spent_prev,
            row_hash=spent_hash,
        )
        .on_conflict_do_nothing(index_elements=["job_id", "direction"])
        .returning(CreditLedger)
    )
    spent_result = await db.execute(spent_stmt)
    spent_row = spent_result.scalar_one_or_none()

    # --- 'earned' row for the contributor's owner --- #
    # Look up prev_row_hash after the spent insert (which may have been a
    # no-op) - if requester == contributor_owner (a self-served job), the
    # spent row is the new latest and the earned row chains onto it.
    # When the spent insert was a genuine insert, the DB has the row but
    # the session hasn't flushed yet, so we construct the chain manually.
    # When the spent insert was a no-op (duplicate call), spent_row is None
    # and the earned insert will also be a no-op; the chain construction
    # below still produces the same hash (same inputs), which is correct.
    if requester_member_id == contributor_owner_member_id:
        earned_prev = spent_hash
    else:
        earned_prev = await _latest_row_hash_for_member(
            db, contributor_owner_member_id
        )

    earned_hash = _compute_row_hash(
        member_id=contributor_owner_member_id,
        direction="earned",
        job_id=job_id,
        model_slug=model_slug,
        output_tokens=output_tokens,
        multiplier=multiplier * CONTRIBUTION_BONUS_MULTIPLIER,
        credits=earned,
        prev_row_hash=earned_prev,
    )

    # Escrow earned credits from cross-member (public-pool) jobs - they are not
    # spendable until a verification result releases them. Self-served /
    # household jobs (requester == contributor owner) are never escrowed: there
    # is no one to defraud when you serve yourself.
    is_public_pool = requester_member_id != contributor_owner_member_id

    # ...but a contributor that has already proven itself (reputation at/above
    # the release threshold) does not need its credits held - escrowing them
    # would just re-trap a trusted peer until the next canary cycle releases
    # them again. Skip escrow when the serving peer is already trusted. Falls
    # back to escrow-on when we weren't told which peer served the job, or the
    # peer has no heartbeat row yet (untrusted by default). The escrowed flag is
    # operational and not part of the row hash, so this never affects the chain.
    escrow = is_public_pool
    if escrow and contributor_peer_id is not None:
        from coordinator.models.peer import PeerHeartbeat

        hb = await db.get(PeerHeartbeat, contributor_peer_id)
        if hb is not None and hb.reputation >= RELEASE_REPUTATION_THRESHOLD:
            escrow = False

    earned_stmt = (
        pg_insert(CreditLedger)
        .values(
            member_id=contributor_owner_member_id,
            direction="earned",
            job_id=job_id,
            model_slug=model_slug,
            output_tokens=output_tokens,
            multiplier=multiplier * CONTRIBUTION_BONUS_MULTIPLIER,
            credits=earned,
            prev_row_hash=earned_prev,
            row_hash=earned_hash,
            escrowed=escrow,
        )
        .on_conflict_do_nothing(index_elements=["job_id", "direction"])
        .returning(CreditLedger)
    )
    earned_result = await db.execute(earned_stmt)
    earned_row = earned_result.scalar_one_or_none()

    # Both inserts were genuine - flush so the rows are visible.
    if spent_row is not None and earned_row is not None:
        await db.flush()
        return spent_row, earned_row

    # At least one insert was a no-op (conflict). The constraint guarantees
    # both rows exist if either does - fetch and return them.
    existing = await _fetch_existing_rows(db, job_id)
    if existing is None:
        # Should be unreachable: the constraint fired but the rows are gone.
        raise RuntimeError(
            f"credit_ledger invariant violated: conflict on job {job_id} "
            "but existing rows not found"
        )
    return existing


async def apply_verification_result(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    passed: bool,
    contributor_peer_id: uuid.UUID | None = None,
) -> bool:
    """Apply a verification outcome for a completed public-pool job - the
    release/forfeit half of credit escrow.

    A verification *source* (sampled re-execution on a second peer, or a
    coordinator canary probe) decides pass/fail and calls this. That source is
    intentionally not wired yet; see the credit-verification design. Until it
    is, escrowed public-pool credits simply stay escrowed (the conservative
    default - don't pay unverified work), which is safe because the public pool
    is not live.

      pass -> release the job's escrowed 'earned' row (escrowed=False) so the
              credits become spendable; nudge the peer's reputation up.
      fail -> leave the 'earned' row escrowed: the credits are forfeited and
              never enter the spendable balance. The append-only ledger keeps
              the row for audit; no clawback row is written. Nudge reputation
              down.

    Returns True if an escrowed earned row was released, else False. Safe to
    call more than once - releasing an already-released row is a no-op.
    """
    earned = (
        await db.execute(
            select(CreditLedger).where(
                CreditLedger.job_id == job_id,
                CreditLedger.direction == "earned",
            )
        )
    ).scalar_one_or_none()

    released = False
    if earned is not None and passed and earned.escrowed:
        earned.escrowed = False  # release -> spendable
        released = True

    if contributor_peer_id is not None:
        # reputation lives on the peer's heartbeat row (preserved across beats -
        # apply_heartbeat's upsert never touches it).
        from coordinator.models.peer import PeerHeartbeat

        hb = await db.get(PeerHeartbeat, contributor_peer_id)
        if hb is not None:
            # This generic per-job path isn't canary-grade-aware, so a False
            # here is treated as an ambiguous miss (soft penalty), never the
            # hard-reset reserved for a confirmed wrong canary answer.
            hb.reputation = nudge_reputation(
                hb.reputation, _OUTCOME_PASS if passed else _OUTCOME_INFRA
            )

    await db.flush()
    return released


async def release_escrow_for_peer(db: AsyncSession, peer_id: uuid.UUID) -> int:
    """Release (escrowed -> spendable) every escrowed 'earned' row from jobs
    this peer served. Returns the number of rows released.

    Used by the verification source once a peer's reputation crosses the trust
    threshold: the peer has proven itself, so the public-pool credits it earned
    while unproven become spendable. The link from a credit row to a peer is via
    the job (credit_ledger.job_id -> jobs.id -> jobs.peer_id).
    """
    from coordinator.models.job import Job

    result = await db.execute(
        update(CreditLedger)
        .where(
            CreditLedger.direction == "earned",
            CreditLedger.escrowed.is_(True),
            CreditLedger.job_id.in_(select(Job.id).where(Job.peer_id == peer_id)),
        )
        .values(escrowed=False)
    )
    await db.flush()
    return result.rowcount or 0
