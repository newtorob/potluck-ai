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


# ---- Job dispatch (m3) ----------------------------------------------- #
#
# Mirrors services/coordinator/coordinator/schemas/peer.py — keep in sync.
#
# Architectural invariant: this contributor process MUST NOT write prompt
# content or generated tokens to disk, logs, or telemetry beyond the
# minimal counters in JobCompleteMessage. CI lint enforces this; do not
# reach for logger.info("prompt=%s", prompt) under any circumstances.


class SamplingParams(BaseModel):
    """Inference sampling parameters. Forward-compatible — older peers
    ignore unknown fields."""

    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float | None = 0.95
    top_k: int | None = None
    repeat_penalty: float | None = None
    stop_sequences: list[str] | None = None
    seed: int | None = None


class JobAssignedMessage(BaseModel):
    """Server → Peer (incoming on this side). Contains the prompt + sampling;
    no member_id or conversation id by design — the coordinator strips
    them before forwarding."""

    type: Literal["job_assigned"]
    job_id: uuid.UUID
    model_slug: str
    prompt: str
    sampling: SamplingParams = Field(default_factory=SamplingParams)


class JobTokenMessage(BaseModel):
    """Peer → Server (outgoing on this side). One token per message during
    streaming; token_index is the peer's local sequence number."""

    type: Literal["job_token"] = "job_token"
    job_id: uuid.UUID
    token_index: int
    token_text: str


class JobCompleteMessage(BaseModel):
    """Peer → Server (outgoing on this side). Sent after the final
    JobTokenMessage. duration_ms is the peer's measured wall-clock from
    receipt of JobAssignedMessage."""

    type: Literal["job_complete"] = "job_complete"
    job_id: uuid.UUID
    output_tokens: int
    duration_ms: int
    finish_reason: Literal["stop", "length", "content_filter", "error"]


class JobErrorMessage(BaseModel):
    """Peer → Server (outgoing on this side). Sent INSTEAD of JobComplete
    when the peer can't fulfill the job. Categorical so the coordinator
    can decide reroute vs. fail."""

    type: Literal["job_error"] = "job_error"
    job_id: uuid.UUID
    error_category: Literal[
        "model_not_loaded",
        "oom",
        "declined",
        "internal",
        "timeout",
    ]
    error_message: str
