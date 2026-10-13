"""Contributor support, coordinator side.

Covers what the in-process desktop contributor needs from the coordinator:
a member session bearer on /peers/connect that binds the member, advertised
throughput persisted on attestation, the per-peer job cap persisted from
heartbeats, the release gate advisory by default and strict on request, the
first-token deadline preferring the measured prompt rate, and the preflight
advisory that names the flag.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.models import MemberKey, Peer, PeerCapability, PeerHeartbeat
from coordinator.services import attestation as attestation_mod


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_app() -> FastAPI:
    from coordinator.db.database import close_db, init_db
    from coordinator.routers.peers import router

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


def _attest(ws, priv: Ed25519PrivateKey, binary_hash: str, *, member_id=None,
            capabilities=None, hardware=None, audiences=None) -> dict:
    """Drive challenge -> attest and return the accept or reject envelope."""
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
    payload = {
        "type": "attest",
        "member_id": str(member_id or uuid.uuid4()),
        "peer_public_key": _b64url(priv.public_key().public_bytes_raw()),
        "binary_hash": binary_hash,
        "client_version": "0.0.2-test",
        "platform": "darwin-arm64",
        "hardware_summary": hardware or {},
        "nonce_signature": _b64url(priv.sign(nonce)),
    }
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if audiences is not None:
        payload["audiences"] = audiences
    ws.send_json(payload)
    return ws.receive_json()


def _pubkey(priv: Ed25519PrivateKey) -> str:
    return _b64url(priv.public_key().public_bytes_raw())


async def _cleanup(*, pubkeys: list[str] = (), members: list[uuid.UUID] = ()) -> None:
    from coordinator.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        async with db.begin():
            for pk in pubkeys:
                await db.execute(text("DELETE FROM member_keys WHERE public_key = :pk"), {"pk": pk})
                await db.execute(text("DELETE FROM peers WHERE public_key = :pk"), {"pk": pk})
            for m in members:
                await db.execute(text("DELETE FROM sessions WHERE member_id = :m"), {"m": m})
                await db.execute(text("DELETE FROM members WHERE id = :m"), {"m": m})


async def _seed_session(member_id: uuid.UUID, raw_token: str) -> None:
    from coordinator.db.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        async with db.begin():
            await db.execute(
                text("INSERT INTO members (id, email) VALUES (:id, :email)"),
                {"id": member_id, "email": f"m25-{uuid.uuid4().hex}@test.local"},
            )
            await db.execute(
                text("INSERT INTO sessions (member_id, token_hash, expires_at) VALUES (:m, :h, :e)"),
                {"m": member_id, "h": hashlib.sha256(raw_token.encode()).hexdigest(),
                 "e": datetime.now(timezone.utc) + timedelta(hours=1)},
            )


async def _peer_row(pubkey: str) -> Peer | None:
    from coordinator.db.database import get_session_factory

    async with get_session_factory()() as db:
        return await db.scalar(select(Peer).where(Peer.public_key == pubkey))


async def _wait_for(predicate, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    return await predicate()


@pytest.fixture()
def no_shared_token(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)


# ---- session bearer on the WebSocket ---------------------------------------


def test_session_bearer_binds_the_member_and_overrides_the_payload(monkeypatch, no_shared_token):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    priv = Ed25519PrivateKey.generate()
    member_id = uuid.uuid4()
    raw_token = f"tok-{uuid.uuid4().hex}"
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        client.portal.call(lambda: _seed_session(member_id, raw_token))
        try:
            with client.websocket_connect(
                "/peers/connect", headers={"Authorization": f"Bearer {raw_token}"}
            ) as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex,
                               member_id=uuid.UUID(int=1))  # the stub the client sends
                assert resp["type"] == "accept", resp
                peer = client.portal.call(lambda: _peer_row(_pubkey(priv)))
                assert peer is not None
                assert peer.member_id == member_id, "session member wins over the payload stub"
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)], members=[member_id]))


def test_bogus_bearer_without_shared_token_is_unauthorized(monkeypatch, no_shared_token):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "real-shared-token-0123456789")
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        with client.websocket_connect(
            "/peers/connect", headers={"Authorization": "Bearer nope"}
        ) as ws:
            first = ws.receive_json()
            # A bearer that does not resolve is refused as such, never downgraded.
            assert first == {"type": "reject", "reason": "unauthorized: invalid session"}


def test_shared_token_still_works_when_session_auth_is_on(monkeypatch):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "real-shared-token-0123456789")
    priv = Ed25519PrivateKey.generate()
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        try:
            with client.websocket_connect(
                "/peers/connect",
                headers={"X-Potluck-Coordinator-Token": "real-shared-token-0123456789"},
            ) as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex)
                assert resp["type"] == "accept", resp
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))


def test_session_member_must_own_a_registered_key_when_keys_are_strict(monkeypatch, no_shared_token):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.setattr(attestation_mod, "get_settings",
                        lambda: Settings(require_registered_keys=True))
    priv = Ed25519PrivateKey.generate()
    member_id = uuid.uuid4()
    raw_token = f"tok-{uuid.uuid4().hex}"
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        client.portal.call(lambda: _seed_session(member_id, raw_token))
        try:
            with client.websocket_connect(
                "/peers/connect", headers={"Authorization": f"Bearer {raw_token}"}
            ) as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex)
                assert resp["type"] == "reject" and "not registered" in resp["reason"]

            async def _register():
                from coordinator.db.database import get_session_factory

                async with get_session_factory()() as db:
                    async with db.begin():
                        db.add(MemberKey(public_key=_pubkey(priv), member_id=member_id,
                                         added_via="session"))

            client.portal.call(_register)
            with client.websocket_connect(
                "/peers/connect", headers={"Authorization": f"Bearer {raw_token}"}
            ) as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex)
                assert resp["type"] == "accept", resp
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)], members=[member_id]))


# ---- advertised throughput + heartbeat cap ---------------------------------


def test_attestation_persists_advertised_throughput(no_shared_token):
    priv = Ed25519PrivateKey.generate()
    slug = f"model-{uuid.uuid4().hex[:8]}-q4_K_M"
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        try:
            with client.websocket_connect("/peers/connect") as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex, capabilities=[{
                    "model_slug": slug, "quantization": "q4_K_M",
                    "tokens_per_second": 41.5, "prompt_tokens_per_second": 380.0,
                }], hardware={"system": "Darwin", "gpu_name": "Apple M2 Pro",
                              "gpu_vram_gb": 16.0, "recommended_tier": "medium"})
                assert resp["type"] == "accept", resp

                async def _cap():
                    from coordinator.db.database import get_session_factory

                    async with get_session_factory()() as db:
                        peer = await db.scalar(select(Peer).where(Peer.public_key == _pubkey(priv)))
                        cap = await db.scalar(select(PeerCapability).where(
                            PeerCapability.peer_id == peer.id, PeerCapability.model_slug == slug))
                        return peer, cap

                peer, cap = client.portal.call(_cap)
                assert cap.tokens_per_second == 41.5
                assert cap.prompt_tokens_per_second == 380.0
                assert peer.hardware_summary["gpu_name"] == "Apple M2 Pro"
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))


def test_heartbeat_persists_cap_and_rejects_absurd_values(no_shared_token):
    priv = Ed25519PrivateKey.generate()
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        try:
            with client.websocket_connect("/peers/connect") as ws:
                resp = _attest(ws, priv, "hash-" + uuid.uuid4().hex)
                assert resp["type"] == "accept", resp
                peer_id, session_id = resp["peer_id"], resp["session_id"]

                def _hb(**extra):
                    ws.send_json({"type": "heartbeat", "peer_id": peer_id, "session_id": session_id,
                                  "timestamp": datetime.now(timezone.utc).isoformat(),
                                  "current_jobs": 0, **extra})

                async def _cap_value():
                    peer = await _peer_row(_pubkey(priv))
                    return peer.max_concurrent_jobs

                _hb(max_concurrent_jobs=2)
                assert client.portal.call(lambda: _wait_for(_cap_value)) == 2
                _hb(max_concurrent_jobs=99)  # over the wire ceiling: invalid, ignored
                _hb()  # absent: keeps the row's value
                _hb(vram_available_gb=5.5)
                import time as _t
                _t.sleep(0.3)
                assert client.portal.call(_cap_value) == 2

                async def _hb_row():
                    from coordinator.db.database import get_session_factory

                    async with get_session_factory()() as db:
                        return await db.scalar(select(PeerHeartbeat).where(
                            PeerHeartbeat.peer_id == uuid.UUID(peer_id)))

                row = client.portal.call(lambda: _wait_for(
                    lambda: _hb_row_if(_hb_row, lambda r: r is not None and r.vram_available_gb == 5.5)))
                assert row.vram_available_gb == 5.5
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))


async def _hb_row_if(getter, ok):
    row = await getter()
    return row if ok(row) else None


# ---- release gate ------------------------------------------------------------


def test_unregistered_release_is_advisory_by_default_and_strict_when_flagged(monkeypatch, no_shared_token):
    priv = Ed25519PrivateKey.generate()
    unknown_hash = "unreleased-dev"
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        try:
            monkeypatch.setattr(attestation_mod, "get_settings",
                                lambda: Settings(require_registered_release=False))
            with client.websocket_connect("/peers/connect") as ws:
                resp = _attest(ws, priv, unknown_hash)
                assert resp["type"] == "accept", resp

                async def _hb_hash():
                    from coordinator.db.database import get_session_factory

                    async with get_session_factory()() as db:
                        row = await db.scalar(select(PeerHeartbeat).where(
                            PeerHeartbeat.peer_id == uuid.UUID(resp["peer_id"])))
                        return row.binary_hash

                assert client.portal.call(_hb_hash) == unknown_hash

            monkeypatch.setattr(attestation_mod, "get_settings",
                                lambda: Settings(require_registered_release=True))
            with client.websocket_connect("/peers/connect") as ws:
                resp = _attest(ws, priv, unknown_hash)
                assert resp["type"] == "reject" and "contributor_releases" in resp["reason"]
        finally:
            client.portal.call(lambda: _cleanup(pubkeys=[_pubkey(priv)]))


# ---- first-token deadline prefers the prompt rate ----------------------------


@pytest.mark.asyncio
async def test_first_token_deadline_prefers_prompt_rate(db_session: AsyncSession) -> None:
    from coordinator.routers import jobs as jobs_router
    from coordinator.routers.jobs import JobRequest, _first_token_deadline
    from coordinator.services.peer_registry import PeerConnection

    slug = f"model-{uuid.uuid4().hex[:8]}"

    async def _peer(tps, pp_tps):
        peer = Peer(member_id=uuid.uuid4(), public_key=uuid.uuid4().hex, status="active",
                    hardware_summary={})
        db_session.add(peer)
        await db_session.flush()
        db_session.add(PeerCapability(peer_id=peer.id, model_slug=slug, quantization="q4",
                                      tokens_per_second=tps, prompt_tokens_per_second=pp_tps))
        await db_session.flush()
        return PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=None,
                              member_id=peer.member_id)

    req = JobRequest(prompt="x" * 4000, model_slug=slug)  # ~1000 tokens
    floor = jobs_router.FIRST_TOKEN_DEADLINE_S
    with_pp = await _first_token_deadline(db_session, req, await _peer(10.0, 1000.0))
    gen_only = await _first_token_deadline(db_session, req, await _peer(10.0, None))
    assert with_pp == pytest.approx(floor + 1.0)
    assert gen_only == pytest.approx(min(floor + 100.0, jobs_router.FIRST_TOKEN_MAX_S))
    assert with_pp < gen_only


# ---- preflight ---------------------------------------------------------------


def test_preflight_release_gate_is_a_named_non_blocking_advisory(monkeypatch):
    from coordinator.services.preflight import run_preflight

    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "coordinator-secret-0123456789")
    base = dict(env="production", require_registered_keys=True,
                database_url="postgresql+asyncpg://app:s3cret@db.internal:5432/potluck?ssl=require")
    off = {c.name: c for c in run_preflight(Settings(**base))}
    assert off["require_registered_release"].status == "warn"
    assert off["require_registered_release"].blocking is False
    assert "REQUIRE_REGISTERED_RELEASE" in off["require_registered_release"].remedy
    on = {c.name: c for c in run_preflight(Settings(**base, require_registered_release=True))}
    assert on["require_registered_release"].status == "ok"
