"""Wire-format schemas — peer side.

Mirrors services/coordinator/coordinator/schemas/peer.py. Keep these two
in sync until we extract a shared library. Any new field added on the
coordinator side must be added here too.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---- Server → Peer ---------------------------------------------------- #


class ChallengeMessage(BaseModel):
    type: Literal["challenge"]
    nonce: str  # base64url, no padding


class AcceptMessage(BaseModel):
    type: Literal["accept"]
    peer_id: uuid.UUID
    session_id: uuid.UUID
    server_time: datetime
    heartbeat_interval_s: int


class RejectMessage(BaseModel):
    type: Literal["reject"]
    reason: str


# ---- Peer → Server ---------------------------------------------------- #


class AttestationPayload(BaseModel):
    type: Literal["attest"] = "attest"
    member_id: uuid.UUID
    peer_public_key: str
    binary_hash: str
    client_version: str
    platform: str
    hostname: str | None = None
    hardware_summary: dict[str, Any] = Field(default_factory=dict)
    nonce_signature: str


class HeartbeatMessage(BaseModel):
    """Sent every `heartbeat_interval_s` seconds after the handshake.

    Coordinator upserts peer_heartbeats with these values. None fields are
    interpreted as "no measurement available" — reasonable when a peer
    has no GPU or hasn't probed yet.
    """

    type: Literal["heartbeat"] = "heartbeat"
    peer_id: uuid.UUID
    session_id: uuid.UUID
    timestamp: datetime
    current_jobs: int = 0
    gpu_utilization: float | None = None
    vram_available_gb: float | None = None
