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
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import select
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
    (spent_row, earned_row) tuple, both freshly inserted + hash-chained.

    Idempotency: callers should ensure this is invoked at most once per
    job (the FK to jobs + the per-job nature of the row makes duplicates
    visible at the DB level if anyone bypasses this contract).
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
    spent_row = CreditLedger(
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
    db.add(spent_row)

    # --- 'earned' row for the contributor's owner --- #
    # Look up prev_row_hash AFTER spent_row is added but BEFORE flush —
    # if requester == contributor_owner (a self-served job), the spent
    # row above is the new latest and our earned row chains onto it.
    # Without a flush, the SELECT won't see the spent row yet, so we
    # construct the chain manually:
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
    earned_row = CreditLedger(
        member_id=contributor_owner_member_id,
        direction="earned",
        job_id=job_id,
        model_slug=model_slug,
        output_tokens=output_tokens,
        multiplier=multiplier * CONTRIBUTION_BONUS_MULTIPLIER,
        credits=earned,
        prev_row_hash=earned_prev,
        row_hash=earned_hash,
    )
    db.add(earned_row)

    await db.flush()
    return spent_row, earned_row
