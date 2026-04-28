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
    RejectMessage,
)
from coordinator.services.attestation import AttestationError, verify_attestation
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

    # ---- post-handshake message loop ----
    # Currently only heartbeat is dispatched; future m3+ adds job lifecycle
    # messages.
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
