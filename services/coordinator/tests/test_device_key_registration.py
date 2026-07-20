"""Tests for POST /me/device-keys — the v0.5 device-key registration path.

Closes the `require_registered_keys=true` onboarding trap: previously a
MemberKey row could only be created via TOFU (disabled in strict mode),
operator SQL backfill, or a seed script — so a brand-new device belonging to
an already-signed-in member could never attest once strict allowlisting was
turned on. `models/member_key.py` has documented the intended fix since it
landed ("v0.5 member-auth ... a signed-in member registers their device key
through the authenticated session, added_via='session'") but no route ever
implemented it until now.

Coverage:
  - happy path: registers a key, added_via='session', bound to the caller.
  - 409 on a key already registered (to the caller, or to a different
    member — either way, no disclosure of which).
  - 400 on a malformed (non-Ed25519) public_key.
  - end-to-end 401 when POTLUCK_SESSION_AUTH_ENABLED=1 and no session is
    presented (real HTTP request against the mounted router + live DB, so
    the actual `resolve_session_member_id` gate is exercised, not just the
    dependency in isolation).
  - registering the key genuinely unblocks the attestation path that FIX 2
    targets: `require_registered_keys=True` + a key registered via this
    endpoint is accepted by `verify_attestation` without TOFU.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.constants import STUB_MEMBER_ID
from coordinator.models.member_key import MemberKey
from coordinator.routers.circles import (
    DeviceKeyRegisterRequest,
    register_device_key,
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _fresh_ed25519_pubkey() -> str:
    return _b64url(Ed25519PrivateKey.generate().public_key().public_bytes_raw())


# ─── direct-call tests (mirrors test_circles.py's pattern) ──────────────────


@pytest.mark.asyncio
async def test_register_device_key_happy_path(db_session: AsyncSession) -> None:
    """register_device_key commits internally (matching create_circle /
    add_circle_member's own convention), so — like those tests — this
    asserts on the returned response rather than re-querying db_session
    afterward: SQLAlchemy won't let the fixture's `session.begin()` wrapper
    be reused once the function under test has committed it closed."""
    pubkey = _fresh_ed25519_pubkey()
    resp = await register_device_key(
        DeviceKeyRegisterRequest(public_key=pubkey, label="my laptop"),
        db=db_session,
        member_id=STUB_MEMBER_ID,
    )
    assert resp.public_key == pubkey
    assert resp.member_id == STUB_MEMBER_ID
    assert resp.label == "my laptop"
    assert resp.added_via == "session"
    assert isinstance(resp.created_at, datetime)


@pytest.mark.asyncio
async def test_register_device_key_happy_path_persists(session_factory) -> None:
    """Same happy path, verified via a fresh session (session_factory
    pattern) so we actually confirm the row landed, not just the response."""
    pubkey = _fresh_ed25519_pubkey()
    try:
        async with session_factory.factory() as db:
            resp = await register_device_key(
                DeviceKeyRegisterRequest(public_key=pubkey),
                db=db,
                member_id=STUB_MEMBER_ID,
            )
        assert resp.public_key == pubkey

        async with session_factory.factory() as verify_db:
            row = await verify_db.get(MemberKey, pubkey)
            assert row is not None
            assert row.member_id == STUB_MEMBER_ID
            assert row.added_via == "session"
    finally:
        async with session_factory.factory() as cleanup_db:
            async with cleanup_db.begin():
                await cleanup_db.execute(
                    text("DELETE FROM member_keys WHERE public_key = :pk"),
                    {"pk": pubkey},
                )


@pytest.mark.asyncio
async def test_register_device_key_no_label_is_none(db_session: AsyncSession) -> None:
    pubkey = _fresh_ed25519_pubkey()
    resp = await register_device_key(
        DeviceKeyRegisterRequest(public_key=pubkey),
        db=db_session,
        member_id=STUB_MEMBER_ID,
    )
    assert resp.label is None


@pytest.mark.asyncio
async def test_register_device_key_409_on_duplicate_same_member(
    db_session: AsyncSession,
) -> None:
    pubkey = _fresh_ed25519_pubkey()
    db_session.add(MemberKey(public_key=pubkey, member_id=STUB_MEMBER_ID, added_via="tofu"))
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await register_device_key(
            DeviceKeyRegisterRequest(public_key=pubkey),
            db=db_session,
            member_id=STUB_MEMBER_ID,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_register_device_key_409_on_duplicate_different_member(
    db_session: AsyncSession,
) -> None:
    """A key already bound to ANOTHER member also 409s — the caller can't
    tell from the response whether it's their own key or someone else's
    (no cross-member disclosure)."""
    other_member = uuid.uuid4()
    pubkey = _fresh_ed25519_pubkey()
    db_session.add(MemberKey(public_key=pubkey, member_id=other_member, added_via="tofu"))
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await register_device_key(
            DeviceKeyRegisterRequest(public_key=pubkey),
            db=db_session,
            member_id=STUB_MEMBER_ID,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_register_device_key_400_on_malformed_key(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc:
        await register_device_key(
            DeviceKeyRegisterRequest(public_key="not-a-real-key"),
            db=db_session,
            member_id=STUB_MEMBER_ID,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_register_device_key_400_on_wrong_length(db_session: AsyncSession) -> None:
    """Valid base64url but not 32 raw bytes -> 400, same as attestation's check."""
    bad = _b64url(secrets.token_bytes(16))
    with pytest.raises(HTTPException) as exc:
        await register_device_key(
            DeviceKeyRegisterRequest(public_key=bad),
            db=db_session,
            member_id=STUB_MEMBER_ID,
        )
    assert exc.value.status_code == 400


# ─── end-to-end: the endpoint is actually gated by session auth ─────────────
#
# Uses the real router + a live DB (docker-compose Postgres on localhost,
# matching every other coordinator test) so the request goes through FastAPI's
# real dependency graph, not a hand-called function — this is what proves the
# route is wired to `resolve_session_member_id` and not, e.g., accidentally
# left on `STUB_MEMBER_ID` passthrough.


def _make_app_with_db():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from coordinator.db.database import close_db, init_db
    from coordinator.routers.circles import router

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await init_db()
        try:
            yield
        finally:
            await close_db()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app


def test_register_device_key_401_without_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    app = _make_app_with_db()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/me/device-keys", json={"public_key": _fresh_ed25519_pubkey()})
    assert resp.status_code == 401


def test_register_device_key_401_with_garbage_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    app = _make_app_with_db()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/me/device-keys",
            json={"public_key": _fresh_ed25519_pubkey()},
            headers={"Authorization": "Bearer totally-bogus"},
        )
    assert resp.status_code == 401


def test_register_device_key_201_with_valid_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real session row + cookie resolves to the real member, and the row
    lands bound to THAT member (not the dev stub)."""
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    app = _make_app_with_db()

    member_id = uuid.uuid4()
    raw_token = f"tok-{uuid.uuid4().hex}"
    pubkey = _fresh_ed25519_pubkey()

    with TestClient(app, raise_server_exceptions=False) as client:
        from coordinator.db.database import get_session_factory

        async def _seed():
            factory = get_session_factory()
            async with factory() as db:
                async with db.begin():
                    await db.execute(
                        text("INSERT INTO members (id, email) VALUES (:id, :email)"),
                        {"id": member_id, "email": f"devkey-{uuid.uuid4().hex}@test.local"},
                    )
                    await db.execute(
                        text(
                            "INSERT INTO sessions (member_id, token_hash, expires_at) "
                            "VALUES (:m, :h, :e)"
                        ),
                        {
                            "m": member_id,
                            "h": hashlib.sha256(raw_token.encode()).hexdigest(),
                            "e": datetime.now(timezone.utc) + timedelta(hours=1),
                        },
                    )

        client.portal.call(_seed)

        try:
            client.cookies.set("potluck_session", raw_token)
            resp = client.post("/me/device-keys", json={"public_key": pubkey})
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["public_key"] == pubkey
            assert body["member_id"] == str(member_id)
            assert body["added_via"] == "session"

            # Duplicate registration by the SAME session -> 409.
            resp2 = client.post("/me/device-keys", json={"public_key": pubkey})
            assert resp2.status_code == 409
        finally:
            async def _cleanup():
                factory = get_session_factory()
                async with factory() as db:
                    async with db.begin():
                        await db.execute(
                            text("DELETE FROM member_keys WHERE public_key = :pk"),
                            {"pk": pubkey},
                        )
                        await db.execute(
                            text("DELETE FROM sessions WHERE member_id = :m"),
                            {"m": member_id},
                        )
                        await db.execute(
                            text("DELETE FROM members WHERE id = :m"),
                            {"m": member_id},
                        )

            client.portal.call(_cleanup)


# ─── registering via this endpoint unblocks strict-mode attestation ─────────


@pytest.mark.asyncio
async def test_registered_key_satisfies_require_registered_keys(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual FIX 2 regression check: with require_registered_keys=True,
    a key registered via /me/device-keys is accepted by verify_attestation
    without ever going through TOFU — reproducing (and closing) the
    onboarding trap described in services/attestation.py.

    Uses session_factory (fresh sessions) throughout: register_device_key
    commits internally, and verify_attestation needs its own uncommitted
    transaction to add + flush the Peer/PeerHeartbeat/PeerCapability rows.
    """
    from coordinator.config import get_settings
    from coordinator.models import ContributorRelease
    from coordinator.schemas.peer import AttestationPayload
    from coordinator.services.attestation import verify_attestation

    get_settings.cache_clear()
    monkeypatch.setenv("REQUIRE_REGISTERED_KEYS", "true")

    priv = Ed25519PrivateKey.generate()
    pubkey_b64 = _b64url(priv.public_key().public_bytes_raw())
    member_id = uuid.uuid4()
    binary_hash = f"hash-{uuid.uuid4().hex}"
    peer_id: uuid.UUID | None = None

    try:
        settings = get_settings()
        assert settings.require_registered_keys is True

        # Register via the endpoint under test — this is the onboarding step.
        async with session_factory.factory() as db:
            await register_device_key(
                DeviceKeyRegisterRequest(public_key=pubkey_b64),
                db=db,
                member_id=member_id,
            )

        async with session_factory.factory() as db:
            async with db.begin():
                db.add(
                    ContributorRelease(
                        binary_hash=binary_hash,
                        version="0.1.0-test",
                        signer_key=f"signer-{uuid.uuid4().hex}",
                    )
                )

        nonce = secrets.token_bytes(32)
        sig = priv.sign(nonce)
        payload = AttestationPayload(
            peer_public_key=pubkey_b64,
            member_id=member_id,
            binary_hash=binary_hash,
            client_version="0.1.0-test",
            platform="linux-x86_64",
            nonce_signature=_b64url(sig),
        )

        async with session_factory.factory() as db:
            async with db.begin():
                peer = await verify_attestation(db, nonce, payload)
                peer_id = peer.id
            assert peer.member_id == member_id
    finally:
        get_settings.cache_clear()
        async with session_factory.factory() as cleanup_db:
            async with cleanup_db.begin():
                if peer_id is not None:
                    await cleanup_db.execute(
                        text("DELETE FROM peer_heartbeats WHERE peer_id = :p"),
                        {"p": peer_id},
                    )
                    await cleanup_db.execute(
                        text("DELETE FROM peer_capabilities WHERE peer_id = :p"),
                        {"p": peer_id},
                    )
                    await cleanup_db.execute(
                        text("DELETE FROM peers WHERE id = :p"), {"p": peer_id}
                    )
                await cleanup_db.execute(
                    text("DELETE FROM member_keys WHERE public_key = :pk"),
                    {"pk": pubkey_b64},
                )
                await cleanup_db.execute(
                    text("DELETE FROM contributor_releases WHERE binary_hash = :h"),
                    {"h": binary_hash},
                )
