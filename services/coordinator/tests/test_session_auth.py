"""Tests for per-session member-identity resolution (WS1 Layer C).

Exercises ``resolve_session_member_id`` - the dependency that swaps the
dev-fixture ``STUB_MEMBER_ID`` for the member behind the caller's login
session by reading the shared ``sessions`` table (owned by services/api).

The ``sessions`` and ``members`` tables live in the same Postgres the
coordinator shares with the cloud API, so these tests insert a member + a
session row via raw SQL inside the rolled-back ``db_session`` transaction and
resolve against them.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.middleware.session_auth import (
    resolve_session_member_id,
    session_auth_enabled,
)


async def _insert_member(db: AsyncSession) -> uuid.UUID:
    member_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO members (id, email) VALUES (:id, :email)"),
        {"id": member_id, "email": f"sess-{uuid.uuid4().hex}@test.local"},
    )
    return member_id


async def _insert_session(
    db: AsyncSession, member_id: uuid.UUID, raw_token: str, *, ttl_hours: float = 1.0
) -> None:
    await db.execute(
        text(
            "INSERT INTO sessions (member_id, token_hash, expires_at) "
            "VALUES (:m, :h, :e)"
        ),
        {
            "m": member_id,
            "h": hashlib.sha256(raw_token.encode()).hexdigest(),
            "e": datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
        },
    )


# ─── the flag helper ─────────────────────────────────────────────────────────


def test_session_auth_enabled_reflects_env(monkeypatch) -> None:
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    assert session_auth_enabled() is False
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    assert session_auth_enabled() is True
    # Only exactly "1" enables it - a stray "true"/"0" stays off (fail-safe).
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "true")
    assert session_auth_enabled() is False
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "0")
    assert session_auth_enabled() is False


# ─── dev passthrough (flag off) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passthrough_returns_stub_when_disabled(
    db_session: AsyncSession, monkeypatch
) -> None:
    """With auth disabled every caller resolves to STUB_MEMBER_ID, even with a
    garbage cookie - the DB is never consulted."""
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    resolved = await resolve_session_member_id(
        potluck_session="totally-bogus", authorization=None, db=db_session
    )
    assert resolved == STUB_MEMBER_ID


# ─── enabled path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_cookie_resolves_member(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    member_id = await _insert_member(db_session)
    raw_token = f"tok-{uuid.uuid4().hex}"
    await _insert_session(db_session, member_id, raw_token)

    resolved = await resolve_session_member_id(
        potluck_session=raw_token, authorization=None, db=db_session
    )
    assert resolved == member_id
    assert resolved != STUB_MEMBER_ID


@pytest.mark.asyncio
async def test_valid_bearer_token_resolves_member(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The desktop app presents the raw token as a bearer credential instead of
    a cookie; the fallback must resolve the same member."""
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    member_id = await _insert_member(db_session)
    raw_token = f"tok-{uuid.uuid4().hex}"
    await _insert_session(db_session, member_id, raw_token)

    resolved = await resolve_session_member_id(
        potluck_session=None, authorization=f"Bearer {raw_token}", db=db_session
    )
    assert resolved == member_id


@pytest.mark.asyncio
async def test_missing_token_401(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    with pytest.raises(HTTPException) as exc:
        await resolve_session_member_id(
            potluck_session=None, authorization=None, db=db_session
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_token_401(db_session: AsyncSession, monkeypatch) -> None:
    """A well-formed token with no matching session row is rejected."""
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    with pytest.raises(HTTPException) as exc:
        await resolve_session_member_id(
            potluck_session=f"tok-{uuid.uuid4().hex}",
            authorization=None,
            db=db_session,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_401(db_session: AsyncSession, monkeypatch) -> None:
    """An expired session row does not resolve (expires_at > now() filter)."""
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    member_id = await _insert_member(db_session)
    raw_token = f"tok-{uuid.uuid4().hex}"
    await _insert_session(db_session, member_id, raw_token, ttl_hours=-1.0)

    with pytest.raises(HTTPException) as exc:
        await resolve_session_member_id(
            potluck_session=raw_token, authorization=None, db=db_session
        )
    assert exc.value.status_code == 401
