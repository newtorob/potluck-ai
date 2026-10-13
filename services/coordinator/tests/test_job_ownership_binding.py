"""job_token / job_complete / job_error must be bound to the connection's
own peer identity.

The job_token/job_complete/job_error handlers in `routers/peers.py` check
that the job was actually dispatched to the sending connection's peer.id,
like the `heartbeat` handler, which rejects a peer_id/session_id mismatch.
Without that check, any connected peer that knew (or guessed) another
peer's job_id could push tokens into that job's buffer, mark it complete
(triggering billing), or abort it early with job_error.

A job_id is a random UUID4 never disclosed to other peers, so this is not
remotely exploitable, but the ownership check keeps the handlers consistent
with the heartbeat check. This test drives the real /peers/connect WebSocket
end-to-end (full attestation handshake, two independently-attested peers)
and asserts:

  1. Peer B sending job_token/job_complete/job_error for a job assigned to
     Peer A is dropped: the buffer is untouched, `done` never fires, no
     billing/finalization side effect occurs.
  2. Peer A acting on its own job still works (regression guard against
     over-tightening the check).
  3. A buffer with `peer_id=None` (legacy/canary path that didn't thread a
     peer_id through register_job) is not gated -- back-compat, matches the
     documented "None case" carve-out.

Requires the docker-compose Postgres (same live-DB convention as
test_device_key_registration.py's end-to-end block) since attestation does
real DB reads/writes (contributor_releases, member_keys, peers,
peer_heartbeats).
"""
from __future__ import annotations

import base64
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from coordinator.services.peer_registry import complete_job, get_job, register_job

_TEST_DB_URL = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"


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


def _attest_and_accept(ws, priv: Ed25519PrivateKey, binary_hash: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Drive one full handshake over an already-open WS connection.
    Returns (peer_id, session_id) from the accept envelope."""
    challenge = ws.receive_json()
    assert challenge["type"] == "challenge"
    nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
    sig = priv.sign(nonce)
    ws.send_json(
        {
            "type": "attest",
            "member_id": str(uuid.uuid4()),
            "peer_public_key": _b64url(priv.public_key().public_bytes_raw()),
            "binary_hash": binary_hash,
            "client_version": "0.1.0-test",
            "platform": "linux-x86_64",
            "nonce_signature": _b64url(sig),
        }
    )
    accept = ws.receive_json()
    assert accept["type"] == "accept", accept
    return uuid.UUID(accept["peer_id"]), uuid.UUID(accept["session_id"])


@pytest.fixture()
def binary_hash():
    return f"hash-{uuid.uuid4().hex}"


@pytest.fixture()
def _release_row(binary_hash):
    """Insert a matching contributor_releases row (a prerequisite of
    verify_attestation) and clean it up after."""

    async def _seed():
        engine = create_async_engine(_TEST_DB_URL, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            async with db.begin():
                await db.execute(
                    text(
                        "INSERT INTO contributor_releases "
                        "(binary_hash, version, signer_key) VALUES (:h, :v, :s)"
                    ),
                    {"h": binary_hash, "v": "0.1.0-test", "s": f"signer-{uuid.uuid4().hex}"},
                )
        await engine.dispose()

    async def _cleanup():
        engine = create_async_engine(_TEST_DB_URL, pool_size=1, max_overflow=0)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            async with db.begin():
                await db.execute(
                    text("DELETE FROM contributor_releases WHERE binary_hash = :h"),
                    {"h": binary_hash},
                )
        await engine.dispose()

    import asyncio

    asyncio.run(_seed())
    yield
    asyncio.run(_cleanup())


def test_job_token_complete_error_are_bound_to_owning_peer(binary_hash, _release_row):
    app = _make_app()
    priv_a = Ed25519PrivateKey.generate()
    priv_b = Ed25519PrivateKey.generate()

    with TestClient(app) as client:
        with client.websocket_connect("/peers/connect") as ws_a, client.websocket_connect(
            "/peers/connect"
        ) as ws_b:
            peer_a_id, _ = _attest_and_accept(ws_a, priv_a, binary_hash)
            peer_b_id, _ = _attest_and_accept(ws_b, priv_b, binary_hash)
            assert peer_a_id != peer_b_id

            async def _mk_job():
                job_id = uuid.uuid4()
                return job_id, await register_job(job_id, peer_id=peer_a_id)

            job_id, buf = client.portal.call(_mk_job)

            try:
                # ---- Peer B (not the assignee) tries to push a token ----
                ws_b.send_json(
                    {
                        "type": "job_token",
                        "job_id": str(job_id),
                        "token_index": 0,
                        "token_text": "intruder-token",
                    }
                )
                # ---- ... and tries to complete it ----
                ws_b.send_json(
                    {
                        "type": "job_complete",
                        "job_id": str(job_id),
                        "output_tokens": 1,
                        "duration_ms": 5,
                        "finish_reason": "stop",
                    }
                )
                # ---- ... and tries to abort it ----
                ws_b.send_json(
                    {
                        "type": "job_error",
                        "job_id": str(job_id),
                        "error_category": "declined",
                        "error_message": "nope",
                    }
                )

                # Give the WS handler's receive loop a moment to process the
                # (rejected) messages before we assert on the buffer.
                import time

                time.sleep(0.3)

                fetched = client.portal.call(lambda: get_job(job_id))
                assert fetched is buf, "job must still be registered -- job_error from the wrong peer must NOT complete it"
                assert buf.tokens == [], "intruder's token must not land in the buffer"
                assert not buf.done.is_set(), "intruder's job_complete/job_error must not finalize the job"
                assert buf.error_category is None
                assert buf.output_tokens == 0

                # ---- Peer A (the real assignee) can still act on its own job ----
                ws_a.send_json(
                    {
                        "type": "job_token",
                        "token_index": 0,
                        "token_text": "hello",
                        "job_id": str(job_id),
                    }
                )
                time.sleep(0.2)
                assert buf.tokens == ["hello"], "owner's token must be accepted"

                ws_a.send_json(
                    {
                        "type": "job_complete",
                        "job_id": str(job_id),
                        "output_tokens": 1,
                        "duration_ms": 5,
                        "finish_reason": "stop",
                    }
                )
                time.sleep(0.2)
                assert buf.done.is_set(), "owner's job_complete must finalize the job"
                assert buf.output_tokens == 1
                assert client.portal.call(lambda: get_job(job_id)) is None
            finally:
                # best-effort cleanup if the assertions above failed early
                client.portal.call(lambda: complete_job(job_id))


def test_job_message_with_no_assigned_peer_is_not_gated(binary_hash, _release_row):
    """Back-compat: a JobBuffer with peer_id=None (legacy/canary path that
    didn't thread a peer_id through register_job) is not subject to the
    ownership check -- any connected peer's message for it is still
    processed, matching pre-fix behaviour for that carve-out."""
    app = _make_app()
    priv = Ed25519PrivateKey.generate()

    with TestClient(app) as client:
        with client.websocket_connect("/peers/connect") as ws:
            _attest_and_accept(ws, priv, binary_hash)

            async def _mk_job():
                job_id = uuid.uuid4()
                return job_id, await register_job(job_id)  # no peer_id

            job_id, buf = client.portal.call(_mk_job)
            try:
                ws.send_json(
                    {
                        "type": "job_token",
                        "job_id": str(job_id),
                        "token_index": 0,
                        "token_text": "hi",
                    }
                )
                import time

                time.sleep(0.2)
                assert buf.tokens == ["hi"]
            finally:
                client.portal.call(lambda: complete_job(job_id))
