"""Peer-facing endpoints. The /peers/connect WebSocket is the contributor
sidecar's lifeline — the handshake gates entry into the network, and (once
the heartbeat task lands) the same connection carries periodic health
updates.
"""
from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.db.database import get_db
from coordinator.schemas.peer import (
    AcceptMessage,
    AttestationPayload,
    ChallengeMessage,
    HeartbeatMessage,
    JobCompleteMessage,
    JobErrorMessage,
    JobTokenMessage,
    RejectMessage,
)
from coordinator.services.attestation import AttestationError, verify_attestation
from coordinator.services.peer_registry import (
    PeerConnection,
    complete_job,
    get_job,
    register_peer,
    unregister_peer,
)
from coordinator.services.peer_state import apply_heartbeat

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["peers"])

# How long to wait for the peer's attestation response after sending the
# challenge. 10s is generous for any real network — anything longer is
# likely a stuck or malicious client.
HANDSHAKE_TIMEOUT_S = 10.0

# How often the peer should send heartbeats once connected. Sent in the
# accept message so we can adjust without a client release. 30s matches
# guide §10.6.
HEARTBEAT_INTERVAL_S = 30


def _new_nonce() -> tuple[bytes, str]:
    """32 cryptographically random bytes + base64url-no-padding form."""
    raw = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return raw, encoded


async def _reject(ws: WebSocket, reason: str, code: int = 1008) -> None:
    """Send a reject envelope and close the connection.

    Best-effort — if the peer is already gone, log and move on.
    """
    try:
        await ws.send_json(RejectMessage(reason=reason).model_dump(mode="json"))
    except Exception:
        pass
    try:
        await ws.close(code=code)
    except Exception:
        pass


@router.websocket("/peers/connect")
async def peer_connect(
    ws: WebSocket,
    db: AsyncSession = Depends(get_db),
) -> None:
    await ws.accept()
    nonce_raw, nonce_b64 = _new_nonce()

    # ---- challenge ----
    try:
        await ws.send_json(ChallengeMessage(nonce=nonce_b64).model_dump(mode="json"))
    except WebSocketDisconnect:
        logger.info("peer_disconnected_before_challenge")
        return

    # ---- await attestation ----
    try:
        raw_msg = await asyncio.wait_for(
            ws.receive_json(), timeout=HANDSHAKE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.info("peer_handshake_timeout")
        await _reject(ws, "attestation timeout", code=1008)
        return
    except WebSocketDisconnect:
        logger.info("peer_disconnected_during_challenge")
        return

    try:
        payload = AttestationPayload.model_validate(raw_msg)
    except ValidationError as e:
        logger.info("peer_attestation_invalid_shape", errors=e.errors())
        await _reject(ws, f"invalid attestation payload: {e.error_count()} error(s)")
        return

    # ---- verify + register ----
    try:
        peer = await verify_attestation(db, nonce_raw, payload)
    except AttestationError as e:
        logger.info(
            "peer_attestation_rejected",
            reason=str(e),
            member_id=str(payload.member_id),
            binary_hash_prefix=payload.binary_hash[:16],
        )
        await db.rollback()
        await _reject(ws, str(e))
        return
    except Exception as e:
        # Anything not an AttestationError is a server bug — don't leak details.
        logger.exception("peer_attestation_internal_error", member_id=str(payload.member_id))
        await db.rollback()
        await _reject(ws, "internal server error during attestation", code=1011)
        return

    await db.commit()

    session_id = uuid.uuid4()
    accept = AcceptMessage(
        peer_id=peer.id,
        session_id=session_id,
        server_time=datetime.now(timezone.utc),
        heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
    )

    try:
        await ws.send_json(accept.model_dump(mode="json"))
    except WebSocketDisconnect:
        logger.info("peer_disconnected_before_accept", peer_id=str(peer.id))
        return

    logger.info(
        "peer_session_opened",
        peer_id=str(peer.id),
        session_id=str(session_id),
        member_id=str(peer.member_id),
        platform=payload.platform,
        client_version=payload.client_version,
    )

    # Register the connection so the job dispatch endpoint can find it.
    await register_peer(
        PeerConnection(
            peer_id=peer.id,
            session_id=session_id,
            ws=ws,
            member_id=peer.member_id,
        )
    )

    # ---- post-handshake message loop ----
    binary_hash = payload.binary_hash
    client_version = payload.client_version

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type") if isinstance(msg, dict) else None

            if msg_type == "heartbeat":
                try:
                    hb = HeartbeatMessage.model_validate(msg)
                except ValidationError as e:
                    logger.warning(
                        "peer_heartbeat_invalid",
                        peer_id=str(peer.id),
                        errors=e.error_count(),
                    )
                    continue
                if hb.peer_id != peer.id or hb.session_id != session_id:
                    # Peer is heartbeating for the wrong session — bug or
                    # malice. Drop the message rather than corrupt state.
                    logger.warning(
                        "peer_heartbeat_session_mismatch",
                        connection_peer_id=str(peer.id),
                        connection_session_id=str(session_id),
                        msg_peer_id=str(hb.peer_id),
                        msg_session_id=str(hb.session_id),
                    )
                    continue
                try:
                    await apply_heartbeat(db, hb, binary_hash, client_version)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "peer_heartbeat_apply_failed", peer_id=str(peer.id)
                    )
                    continue
                logger.debug(
                    "peer_heartbeat_applied",
                    peer_id=str(peer.id),
                    session_id=str(session_id),
                    current_jobs=hb.current_jobs,
                )
            elif msg_type == "job_token":
                try:
                    tok = JobTokenMessage.model_validate(msg)
                except ValidationError:
                    logger.warning("peer_job_token_invalid", peer_id=str(peer.id))
                    continue
                buf = await get_job(tok.job_id)
                if buf is None:
                    # Token for a job we don't know about (cancelled? leak?).
                    logger.debug(
                        "peer_job_token_orphan",
                        peer_id=str(peer.id),
                        job_id=str(tok.job_id),
                    )
                    continue
                buf.tokens.append(tok.token_text)
                # Also push to the streaming queue. put_nowait() can raise
                # QueueFull on a runaway peer; we log + drop in that case
                # rather than block the WS receive loop. Streaming consumer
                # would see a brief gap; sync consumer is unaffected.
                try:
                    buf.token_stream.put_nowait(tok.token_text)
                except asyncio.QueueFull:
                    logger.warning(
                        "peer_job_token_queue_full",
                        peer_id=str(peer.id),
                        job_id=str(tok.job_id),
                    )
            elif msg_type == "job_complete":
                try:
                    cmp = JobCompleteMessage.model_validate(msg)
                except ValidationError:
                    logger.warning("peer_job_complete_invalid", peer_id=str(peer.id))
                    continue
                buf = await get_job(cmp.job_id)
                if buf is None:
                    continue
                buf.output_tokens = cmp.output_tokens
                buf.duration_ms = cmp.duration_ms
                buf.finish_reason = cmp.finish_reason
                await complete_job(cmp.job_id)
                logger.info(
                    "peer_job_complete",
                    peer_id=str(peer.id),
                    job_id=str(cmp.job_id),
                    output_tokens=cmp.output_tokens,
                    duration_ms=cmp.duration_ms,
                )
            elif msg_type == "job_error":
                try:
                    err = JobErrorMessage.model_validate(msg)
                except ValidationError:
                    logger.warning("peer_job_error_invalid", peer_id=str(peer.id))
                    continue
                buf = await get_job(err.job_id)
                if buf is None:
                    continue
                buf.error_category = err.error_category
                buf.error_message = err.error_message
                await complete_job(err.job_id)
                logger.info(
                    "peer_job_error",
                    peer_id=str(peer.id),
                    job_id=str(err.job_id),
                    error_category=err.error_category,
                )
            else:
                logger.debug(
                    "peer_message_unhandled",
                    peer_id=str(peer.id),
                    session_id=str(session_id),
                    msg_type=msg_type,
                )
    except WebSocketDisconnect:
        logger.info(
            "peer_session_closed",
            peer_id=str(peer.id),
            session_id=str(session_id),
        )
    finally:
        await unregister_peer(peer.id)
