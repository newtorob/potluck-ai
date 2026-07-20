"""Verify a peer's attestation payload and register/refresh the peer row.

Called from the WS handshake after the peer answers the server's nonce
challenge. Returns the upserted Peer on success, raises AttestationError
on any failure (caller turns that into a `reject` message + WS close).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import get_settings
from coordinator.crypto import KeyDecodeError, b64url_decode, decode_ed25519_public_key
from coordinator.models import (
    ContributorRelease,
    MemberKey,
    Peer,
    PeerCapability,
    PeerHeartbeat,
)
from coordinator.schemas.peer import AttestationPayload

logger = structlog.get_logger(__name__)


class AttestationError(Exception):
    """Raised when attestation fails for any reason. Message is safe to
    relay to the peer in a `reject` envelope."""


def _b64url_decode(s: str) -> bytes:
    """Decode base64url with optional padding stripped. Thin re-export of
    `coordinator.crypto.b64url_decode` kept for callers already importing
    this name from this module."""
    return b64url_decode(s)


def _decode_public_key(b64: str) -> Ed25519PublicKey:
    """Decode + validate `peer_public_key`. Delegates to the shared
    `coordinator.crypto` helper (also used by the device-key registration
    endpoint and the DERP-token PoP check) and re-raises as AttestationError
    so existing callers/tests see the same exception type as before."""
    try:
        return decode_ed25519_public_key(b64, field_name="peer_public_key")
    except KeyDecodeError as e:
        raise AttestationError(str(e)) from e


async def verify_attestation(
    db: AsyncSession,
    nonce: bytes,
    payload: AttestationPayload,
) -> Peer:
    """Verify the attestation payload end-to-end and upsert the Peer row.

    Order of checks (cheapest → most expensive):
      1. Decode + validate the peer public key (32 raw bytes, Ed25519)
      2. Verify the nonce signature
      3. Look up binary_hash in contributor_releases (DB hit)
      4. Find or register the peer (DB hit)
      5. Update peer_heartbeats with the connection metadata (DB hit)

    Steps 4 and 5 happen inside the caller's transaction; the caller is
    responsible for `await db.commit()` after this returns.
    """
    pub = _decode_public_key(payload.peer_public_key)

    try:
        sig = _b64url_decode(payload.nonce_signature)
    except Exception as e:
        raise AttestationError(f"nonce_signature: not valid base64url ({e})") from e

    try:
        pub.verify(sig, nonce)
    except InvalidSignature as e:
        raise AttestationError("nonce_signature: does not verify") from e

    # binary_hash is a known-release gate, not proof the peer is actually
    # running that binary — a peer can present any released hash it has seen.
    # Treat it as advisory defence-in-depth; real binary attestation (TEE) is
    # a later hardening item.
    release = await db.scalar(
        select(ContributorRelease).where(
            ContributorRelease.binary_hash == payload.binary_hash
        )
    )
    if release is None:
        raise AttestationError(
            f"binary_hash {payload.binary_hash[:16]}…: not in contributor_releases"
        )

    peer = await db.scalar(
        select(Peer).where(Peer.public_key == payload.peer_public_key)
    )

    # ---- member <-> key binding ---- #
    # The nonce signature above proved the peer holds the private key for
    # peer_public_key. Bind that key to a member so member_id can't be freely
    # self-asserted: a known key MUST attest as the member it is pinned to.
    binding = await db.scalar(
        select(MemberKey).where(MemberKey.public_key == payload.peer_public_key)
    )
    if binding is not None:
        if binding.member_id != payload.member_id:
            raise AttestationError(
                "peer_public_key is registered to a different member"
            )
        bound_member_id = binding.member_id
    elif peer is not None:
        # Peer row predates the binding table: its established member_id is
        # authoritative. Backfill the binding from it.
        bound_member_id = peer.member_id
        db.add(
            MemberKey(
                public_key=payload.peer_public_key,
                member_id=bound_member_id,
                added_via="backfill",
            )
        )
        await db.flush()
    elif get_settings().require_registered_keys:
        raise AttestationError(
            "peer_public_key is not registered for any member — "
            "register this device key before connecting"
        )
    else:
        # New key, lenient mode: trust-on-first-use, pin to the claimed member.
        bound_member_id = payload.member_id
        db.add(
            MemberKey(
                public_key=payload.peer_public_key,
                member_id=bound_member_id,
                added_via="tofu",
            )
        )
        await db.flush()
        logger.info("member_key_tofu_registered", member_id=str(bound_member_id))

    now = datetime.now(timezone.utc)

    if peer is None:
        peer = Peer(
            member_id=bound_member_id,
            public_key=payload.peer_public_key,
            hostname=payload.hostname,
            hardware_summary=payload.hardware_summary,
            last_seen_at=now,
            status="active",
        )
        db.add(peer)
        await db.flush()
        logger.info(
            "peer_registered",
            peer_id=str(peer.id),
            member_id=str(peer.member_id),
            release_version=release.version,
            platform=payload.platform,
        )
    else:
        peer.last_seen_at = now
        peer.hostname = payload.hostname
        peer.hardware_summary = payload.hardware_summary
        if peer.status == "pending":
            peer.status = "active"
        await db.flush()
        logger.info(
            "peer_reconnected",
            peer_id=str(peer.id),
            member_id=str(peer.member_id),
            release_version=release.version,
        )

    # Upsert peer_heartbeats — primary key is peer_id, so on_conflict_do_update.
    hb_stmt = (
        pg_insert(PeerHeartbeat)
        .values(
            peer_id=peer.id,
            last_beat_at=now,
            current_jobs=0,
            reputation=0.5,
            binary_hash=payload.binary_hash,
            client_version=payload.client_version,
        )
        .on_conflict_do_update(
            index_elements=["peer_id"],
            set_={
                "last_beat_at": now,
                "binary_hash": payload.binary_hash,
                "client_version": payload.client_version,
            },
        )
    )
    await db.execute(hb_stmt)

    # Refresh peer_capabilities. Capabilities can change between sessions
    # (peer pulled a new model, swapped models, dropped to stub-only) so
    # we delete-and-replace rather than merge.
    await db.execute(
        delete(PeerCapability).where(PeerCapability.peer_id == peer.id)
    )
    for cap in payload.capabilities:
        db.add(PeerCapability(
            peer_id=peer.id,
            model_slug=cap.model_slug,
            quantization=cap.quantization,
        ))
    if payload.capabilities:
        logger.info(
            "peer_capabilities_registered",
            peer_id=str(peer.id),
            model_slugs=[c.model_slug for c in payload.capabilities],
        )

    return peer
