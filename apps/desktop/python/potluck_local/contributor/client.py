"""Contributor sidecar — WebSocket client + lifecycle.

Connects to the coordinator's /peers/connect endpoint, completes the
Ed25519 attestation handshake, then sends periodic heartbeats. Reconnects
on any failure with exponential backoff.

Run as `python -m potluck_local.contributor`. Settings come from
POTLUCK_* environment variables (see config.py).

Architectural invariant (see manifesto §10.3): this process must never
write prompt content or member-identifying data to disk, logs, or
telemetry. The handshake + heartbeat loop here only handles routing
metadata. When job dispatch lands in m3-m4, the same invariant applies
to the inference path.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import platform as _platform
import socket
import sys
import uuid
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potluck_local.contributor.config import Settings, get_settings
from potluck_local.contributor.keys import ensure_keypair, public_key_b64
from potluck_local.contributor.schemas import (
    AcceptMessage,
    AttestationPayload,
    ChallengeMessage,
    HeartbeatMessage,
    RejectMessage,
)

logger = logging.getLogger("potluck.contributor")

# Module version — embedded in attestation, persisted by coordinator into
# peer_heartbeats.client_version. Bump when the wire protocol or
# heartbeat shape changes.
CLIENT_VERSION = "0.0.1-dev"

# How long to wait for an accept/reject after sending attestation.
HANDSHAKE_RESPONSE_TIMEOUT_S = 10.0

# Reconnect backoff bounds.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0


class HandshakeFailed(Exception):
    """Wraps a coordinator reject — caller treats this as fatal vs.
    transient based on context."""


def detect_platform() -> str:
    """Cross-platform identifier, e.g. 'darwin-arm64', 'linux-x86_64'.

    Note Windows reports machine() as 'AMD64' rather than 'x86_64' — we
    keep what platform.machine() returns rather than normalizing, so the
    string is faithful to what the underlying OS calls itself. Coordinator
    treats this as informational, not a routing key.
    """
    return f"{_platform.system().lower()}-{_platform.machine().lower()}"


def detect_hardware_summary() -> dict[str, object]:
    """Minimal hardware metadata. Real GPU detection lands in m3+ when the
    coordinator actually routes by capability. For m1 we capture enough
    to make the /transparency/fleet view useful (cpu count, OS release)."""
    summary: dict[str, object] = {
        "system": _platform.system(),
        "release": _platform.release(),
        "machine": _platform.machine(),
        "cpu_count": _platform.os.cpu_count() if hasattr(_platform, "os") else None,
    }
    return summary


async def _do_handshake(
    ws: websockets.WebSocketClientProtocol,
    settings: Settings,
    priv: Ed25519PrivateKey,
) -> AcceptMessage:
    # 1) receive challenge
    raw = await ws.recv()
    challenge = ChallengeMessage.model_validate_json(raw)
    nonce_bytes = base64.urlsafe_b64decode(
        challenge.nonce + "=" * (-len(challenge.nonce) % 4)
    )

    # 2) sign + send attest
    sig = priv.sign(nonce_bytes)
    attest = AttestationPayload(
        member_id=settings.member_id,
        peer_public_key=public_key_b64(priv.public_key()),
        binary_hash=settings.binary_hash,
        client_version=CLIENT_VERSION,
        platform=detect_platform(),
        hostname=socket.gethostname(),
        hardware_summary=detect_hardware_summary(),
        nonce_signature=base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
    )
    await ws.send(attest.model_dump_json())

    # 3) receive accept or reject
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_RESPONSE_TIMEOUT_S)
    msg = json.loads(raw)
    msg_type = msg.get("type")
    if msg_type == "accept":
        return AcceptMessage.model_validate(msg)
    if msg_type == "reject":
        reject = RejectMessage.model_validate(msg)
        raise HandshakeFailed(reject.reason)
    raise HandshakeFailed(f"unexpected post-attestation message: {msg_type}")


async def _heartbeat_loop(
    ws: websockets.WebSocketClientProtocol,
    accept: AcceptMessage,
) -> None:
    """Send a heartbeat every `heartbeat_interval_s`. Returns when the
    connection closes; caller handles reconnect."""
    while True:
        await asyncio.sleep(accept.heartbeat_interval_s)
        hb = HeartbeatMessage(
            peer_id=accept.peer_id,
            session_id=accept.session_id,
            timestamp=datetime.now(timezone.utc),
            current_jobs=0,
            gpu_utilization=None,
            vram_available_gb=None,
        )
        await ws.send(hb.model_dump_json())
        logger.debug("heartbeat_sent peer_id=%s", accept.peer_id)


async def _session(settings: Settings, priv: Ed25519PrivateKey) -> None:
    logger.info("connecting url=%s", settings.coordinator_url)
    async with websockets.connect(settings.coordinator_url) as ws:
        try:
            accept = await _do_handshake(ws, settings, priv)
        except HandshakeFailed as e:
            logger.error("handshake_rejected reason=%s", e)
            # Reject is "fatal for this attempt" — most likely an unknown
            # binary_hash, bad sig, or member_id problem. Reconnecting
            # without changing config will just fail the same way; surface
            # the error and let the backoff slow us down.
            raise
        logger.info(
            "session_opened peer_id=%s session_id=%s heartbeat_interval=%ds",
            accept.peer_id,
            accept.session_id,
            accept.heartbeat_interval_s,
        )
        await _heartbeat_loop(ws, accept)


async def _run(settings: Settings, priv: Ed25519PrivateKey) -> None:
    backoff = RECONNECT_BACKOFF_INITIAL_S
    while True:
        try:
            await _session(settings, priv)
            backoff = RECONNECT_BACKOFF_INITIAL_S
        except HandshakeFailed:
            logger.warning("retry_after_reject in_s=%.1f", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
        except (
            ConnectionRefusedError,
            websockets.ConnectionClosed,
            websockets.WebSocketException,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            logger.warning("disconnected error=%s retry_in_s=%.1f", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="potluck-contributor",
        description=(
            "Potluck contributor sidecar. Connects to the coordinator, "
            "attests with an Ed25519 keypair, and heartbeats. No inference "
            "yet — that's m3+."
        ),
    )
    p.add_argument(
        "--verbose", "-v", action="store_true", help="DEBUG-level logging"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    settings = get_settings()

    if not settings.binary_hash:
        logger.error(
            "missing POTLUCK_BINARY_HASH — set it to the SHA256 of the "
            "current contributor binary, which must be present in the "
            "coordinator's contributor_releases table"
        )
        sys.exit(2)

    priv = ensure_keypair(settings.key_dir)
    pub_b64 = public_key_b64(priv.public_key())
    logger.info(
        "contributor_starting member_id=%s peer_pubkey=%s binary_hash=%s…",
        settings.member_id,
        pub_b64,
        settings.binary_hash[:16],
    )

    try:
        asyncio.run(_run(settings, priv))
    except KeyboardInterrupt:
        logger.info("shutdown_requested")


if __name__ == "__main__":
    main()
