"""Token-cap tests: billing is bounded by the job's requested
sampling.max_tokens, so a peer that over-generates can't inflate credits.

The cap itself lives in routers/peers.py (job_complete):
    billed = min(len(buf.tokens), buf.max_tokens)
These tests cover the plumbing that makes the cap possible - register_job
threads the job's max_tokens onto the JobBuffer that job_complete reads -
plus the cap arithmetic as the billing contract. No DB needed: the job
registry is in-memory.
"""
from __future__ import annotations

import uuid

import pytest

from coordinator.schemas.peer import MAX_OUTPUT_TOKENS
from coordinator.services.peer_registry import (
    complete_job,
    get_job,
    register_job,
)


@pytest.mark.asyncio
async def test_register_job_threads_max_tokens_onto_buffer():
    """The dispatch path passes sampling.max_tokens; job_complete must see it."""
    job_id = uuid.uuid4()
    await register_job(job_id, max_tokens=512)
    try:
        fetched = await get_job(job_id)
        assert fetched is not None
        assert fetched.max_tokens == 512
    finally:
        await complete_job(job_id)


@pytest.mark.asyncio
async def test_register_job_defaults_to_hard_ceiling():
    """A job registered without an explicit budget caps at the hard ceiling,
    so the per-job cap is a no-op rather than over-restricting."""
    job_id = uuid.uuid4()
    buf = await register_job(job_id)
    try:
        assert buf.max_tokens == MAX_OUTPUT_TOKENS
    finally:
        await complete_job(job_id)


@pytest.mark.asyncio
async def test_billing_caps_overgeneration_to_budget():
    """Billing contract: a peer that ignores max_tokens=512 and streams 4096
    is billed for 512 (the budget), not 4096; an honest under-budget response
    bills exactly what it produced."""
    job_id = uuid.uuid4()
    buf = await register_job(job_id, max_tokens=512)
    try:
        buf.tokens.extend(["x"] * 4096)  # peer over-generated past the budget
        counted = len(buf.tokens)
        billed = min(counted, buf.max_tokens)
        assert counted == 4096
        assert billed == 512  # capped to the requested budget
        # under-budget: billed == produced
        assert min(300, buf.max_tokens) == 300
    finally:
        await complete_job(job_id)
