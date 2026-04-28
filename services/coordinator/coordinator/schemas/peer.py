"""Wire-format schemas for the contributor → coordinator handshake.

Mirrored on the contributor sidecar side (apps/desktop/python/potluck_local/
contributor/schemas.py — built in the next task). Keep these two in sync
until we extract a shared library.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---- Server → Peer ---------------------------------------------------- #


class ChallengeMessage(BaseModel):
    type: Literal["challenge"] = "challenge"
    nonce: str  # base64url(32 random bytes), no padding


class AcceptMessage(BaseModel):
    type: Literal["accept"] = "accept"
    peer_id: uuid.UUID
    session_id: uuid.UUID
    server_time: datetime
    # heartbeat_interval_s tells the peer how often to send heartbeats. The
    # coordinator owns this so we can tune fleet load without shipping a
    # client update.
    heartbeat_interval_s: int = 30


class RejectMessage(BaseModel):
    type: Literal["reject"] = "reject"
    reason: str


# ---- Peer → Server ---------------------------------------------------- #


class AttestationPayload(BaseModel):
    """The peer's response to the server's challenge.

    Identity proof:
      - `peer_public_key` — the peer's per-peer Ed25519 public key
        (distinct from the release-signing key)
      - `nonce_signature` — Ed25519 signature of the server's nonce bytes,
        computed with the per-peer private key
    Binary attestation:
      - `binary_hash` — SHA256 of the contributor release artifact the peer
        is running. Coordinator looks this up in `contributor_releases` and
        rejects unknown hashes.
    """

    type: Literal["attest"] = "attest"
    member_id: uuid.UUID
    peer_public_key: str  # base64url, no padding
    binary_hash: str  # SHA256 hex (lowercase)
    client_version: str
    platform: str  # e.g. "darwin-arm64", "linux-x86_64", "windows-x86_64"
    hostname: str | None = None
    hardware_summary: dict[str, Any] = Field(default_factory=dict)
    nonce_signature: str  # base64url, no padding


class HeartbeatMessage(BaseModel):
    """Sent every `heartbeat_interval_s` seconds after the handshake.

    Coordinator upserts peer_heartbeats with these values + bumps
    peers.last_seen_at. None numeric fields are interpreted as "no
    measurement available" (e.g. peer has no GPU, or hasn't probed yet).
    """

    type: Literal["heartbeat"]
    peer_id: uuid.UUID
    session_id: uuid.UUID
    timestamp: datetime
    current_jobs: int = 0
    gpu_utilization: float | None = None
    vram_available_gb: float | None = None
