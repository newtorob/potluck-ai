"""Credit-ledger write path.

`record_job_credits` is the single function any inference-finalizer
calls when a job completes successfully. It writes two rows (one
`earned` for the contributor's owner, one `spent` for the requester),
both snapshotting the model-class multiplier at write time, and both
chained via SHA-256 to the previous row for that member.

Asymmetric earn/spend (memory `b408b3a5`):

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
function twice for the same job is a safe no-op — the second call
returns the rows that were created by the first call, identical to what
the first call returned.
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.credit import CreditLedger, CreditModelMultiplier

# Asymmetric earn/spend multiplier. The 1.5x reward for serving (vs.
# consuming) is what makes "run a peer 24/7" the dominant strategy on
# net balance. See memory `b408b3a5` for the cooperative-economics
# reasoning + the torrent-ratio analogy.
CONTRIBUTION_BONUS_MULTIPLIER = Decimal("1.5")

# Fallback multiplier when a job ran on a model_slug we don't have in
# `credit_model_multipliers`. Conservative — we'd rather under-credit
# than over-credit an unknown model. A warning is logged + the slug
# should be added to the table.
UNKNOWN_MODEL_MULTIPLIER = Decimal("1.0")

# Constant used for the very first row in a member's chain.
GENESIS_HASH = hashlib.sha256(b"potluck-credit-ledger-genesis").hexdigest()

# Reputation nudge per verification outcome. A peer's reputation (0..1, default
# 0.5) moves toward 1.0 on a passed verification and toward 0.0 on a failure.
REPUTATION_STEP = 0.1

# Reputation at/above which a peer is trusted enough to release its escrowed
# earned credits (built up via passed canary verifications). Default rep is 0.5,
# so two consecutive passed canaries (0.5 -> 0.6 -> 0.7) cross it.
RELEASE_REPUTATION_THRESHOLD = 0.7


def nudge_reputation(current: float, passed: bool) -> float:
    """Move a reputation score (0..1) one REPUTATION_STEP toward 1.0 on a passed
    verification, toward 0.0 on a failure. Clamped to [0, 1]."""
    step = REPUTATION_STEP if passed else -REPUTATION_STEP
    return max(0.0, min(1.0, current + step))


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

    Stability requires ordered-field serialization — never use a Python
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
) -> tuple[CreditLedger, CreditLedger]:
    """Write the two ledger rows for a completed job. Returns the
    (spent_row, earned_row) tuple.

    Idempotent: calling this function twice for the same job_id is safe.
    The second call detects the conflict via UNIQUE (job_id, direction),
    skips the insert, and returns the rows created by the first call.
    The hash chain is unaffected — the no-op insert does not write a
    new row, so the chain state is identical to what the first call
    produced.
    """
    if output_tokens <= 0:
        # No credit for zero-output jobs. Callers shouldn't invoke us
        # in that case; we accept and silently no-op to be defensive.
        raise ValueError(
            f"refusing to credit job {job_id} with output_tokens={output_tokens}"
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
    # Look up prev_row_hash AFTER the spent insert (which may have been a
    # no-op) — if requester == contributor_owner (a self-served job), the
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

    # Escrow earned credits from cross-member (public-pool) jobs — they are not
    # spendable until a verification result releases them. Self-served /
    # household jobs (requester == contributor owner) are never escrowed: there
    # is no one to defraud when you serve yourself.
    is_public_pool = requester_member_id != contributor_owner_member_id

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
            escrowed=is_public_pool,
        )
        .on_conflict_do_nothing(index_elements=["job_id", "direction"])
        .returning(CreditLedger)
    )
    earned_result = await db.execute(earned_stmt)
    earned_row = earned_result.scalar_one_or_none()

    # Both inserts were genuine — flush so the rows are visible.
    if spent_row is not None and earned_row is not None:
        await db.flush()
        return spent_row, earned_row

    # At least one insert was a no-op (conflict). The constraint guarantees
    # both rows exist if either does — fetch and return them.
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
    """Apply a verification outcome for a completed public-pool job — the
    release/forfeit half of credit escrow.

    A verification *source* (sampled re-execution on a second peer, or a
    coordinator canary probe) decides pass/fail and calls this. That source is
    intentionally not wired yet; see the credit-verification design. Until it
    is, escrowed public-pool credits simply stay escrowed (the conservative
    default — don't pay unverified work), which is safe because the public pool
    is not live.

      pass -> release the job's escrowed 'earned' row (escrowed=False) so the
              credits become spendable; nudge the peer's reputation up.
      fail -> leave the 'earned' row escrowed: the credits are forfeited and
              never enter the spendable balance. The append-only ledger keeps
              the row for audit; no clawback row is written. Nudge reputation
              down.

    Returns True if an escrowed earned row was released, else False. Safe to
    call more than once — releasing an already-released row is a no-op.
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
        # reputation lives on the peer's heartbeat row (preserved across beats —
        # apply_heartbeat's upsert never touches it).
        from coordinator.models.peer import PeerHeartbeat

        hb = await db.get(PeerHeartbeat, contributor_peer_id)
        if hb is not None:
            hb.reputation = nudge_reputation(hb.reputation, passed)

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
