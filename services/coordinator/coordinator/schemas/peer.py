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


class CapabilityEntry(BaseModel):
    """One model the peer is willing to serve. Coordinator uses these to
    route jobs only to peers that actually have the requested model
    loaded (or pullable from local storage). Empty list = peer accepts
    no inference jobs (stub-echo only)."""

    model_slug: str  # canonical name, e.g. "qwen2.5:3b-instruct-q4_K_M"
    quantization: str = "unknown"  # e.g. "q4_K_M", "q5_K_M", "f16"


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
    Capability advertisement (m3 step #18):
      - `capabilities` — list of (model_slug, quantization) the peer can
        serve. Coordinator uses this to populate peer_capabilities and
        route jobs by model. Default empty for backwards compat with
        stub-echo peers that don't run inference.
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
    capabilities: list[CapabilityEntry] = Field(default_factory=list)


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


# ---- Job dispatch (m3) ----------------------------------------------- #
#
# Privacy invariant (manifesto §10.3, memory 42a7e671):
#   - JobAssignedMessage MUST NOT carry member_id, source_conversation_id,
#     or any identifier that links the prompt back to its requester. The
#     coordinator strips these before sending. CI lint enforces this on
#     peer-bound code paths.
#   - Peer-side: prompt + tokens MUST NOT be written to disk, logs, or
#     telemetry. In-memory only.


class SamplingParams(BaseModel):
    """Inference sampling parameters. Forward-compatible — add fields here
    rather than to JobAssignedMessage so older peers ignore unknown ones."""

    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float | None = 0.95
    top_k: int | None = None
    repeat_penalty: float | None = None
    stop_sequences: list[str] | None = None
    seed: int | None = None


class JobAssignedMessage(BaseModel):
    """Server → Peer: 'run inference on this prompt and stream tokens back.'

    Carries ONLY the routing-blind subset: job id, model slug, prompt,
    sampling params. No member_id, no conversation id, no anything that
    could identify the requester."""

    type: Literal["job_assigned"] = "job_assigned"
    job_id: uuid.UUID
    model_slug: str  # e.g. "llama-3.2-3b-instruct-q4_k_m"
    prompt: str
    sampling: SamplingParams = Field(default_factory=SamplingParams)


class JobTokenMessage(BaseModel):
    """Peer → Server: a single token (or short token batch) of the streaming
    response. token_index lets the coordinator detect gaps + reorder if a
    future transport drops ordering."""

    type: Literal["job_token"]
    job_id: uuid.UUID
    token_index: int
    token_text: str


class JobCompleteMessage(BaseModel):
    """Peer → Server: inference finished cleanly. duration_ms is the peer's
    measured wall-clock from job_assigned receipt to last token; coordinator
    persists it on the job row."""

    type: Literal["job_complete"]
    job_id: uuid.UUID
    output_tokens: int
    duration_ms: int
    finish_reason: Literal["stop", "length", "content_filter", "error"]


class JobErrorMessage(BaseModel):
    """Peer → Server: peer can't fulfill this job. Categorical reason lets
    the coordinator decide whether to reroute (e.g. oom → reroute) vs. fail
    the request (e.g. declined → fail)."""

    type: Literal["job_error"]
    job_id: uuid.UUID
    error_category: Literal[
        "model_not_loaded",
        "oom",
        "declined",
        "internal",
        "timeout",
    ]
    error_message: str  # free-form, peer is responsible for not leaking PII
