"""Wire-format schemas for the contributor → coordinator handshake.

Mirrored on the contributor sidecar side (apps/desktop/python/potluck_local/
contributor/schemas.py — built in the next task). Keep these two in sync
until we extract a shared library.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    # The audiences from the attestation that this coordinator enforces,
    # echoed so the peer can tell its choice is honoured. A peer that asked
    # to serve circles only and does not see that here is talking to a
    # coordinator from before audiences existed and must not serve. None for
    # a peer that named no audiences.
    audiences: list[str] | None = None


class RejectMessage(BaseModel):
    type: Literal["reject"] = "reject"
    reason: str


# ---- Peer → Server ---------------------------------------------------- #


# Measured speeds are a peer's own claim and they steer routing, so they must be
# finite. Values above these ceilings are clamped rather than refused, so a
# fast honest peer still attests.
MAX_TOKENS_PER_SECOND = 1000.0
MAX_PROMPT_TOKENS_PER_SECOND = 100_000.0


class CapabilityEntry(BaseModel):
    """One model the peer is willing to serve. Coordinator uses these to
    route jobs only to peers that actually have the requested model
    loaded (or pullable from local storage). Empty list = peer accepts
    no inference jobs (stub-echo only)."""

    model_slug: str  # canonical name, e.g. "qwen2.5:3b-instruct-q4_K_M"
    quantization: str = "unknown"  # e.g. "q4_K_M", "q5_K_M", "f16"
    # Measured on the peer for this model: generation speed and prompt-
    # processing speed. None until the peer has benchmarked the model. The
    # scorer uses tokens_per_second; the first-token deadline prefers
    # prompt_tokens_per_second. Both are peer self-reports (advisory, like
    # every other capacity figure): they steer routing, never billing.
    tokens_per_second: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    prompt_tokens_per_second: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("tokens_per_second")
    @classmethod
    def _cap_generation_rate(cls, value: float | None) -> float | None:
        return None if value is None else min(value, MAX_TOKENS_PER_SECOND)

    @field_validator("prompt_tokens_per_second")
    @classmethod
    def _cap_prompt_rate(cls, value: float | None) -> float | None:
        return None if value is None else min(value, MAX_PROMPT_TOKENS_PER_SECOND)


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
    # Who this peer agrees to serve beyond its owner's own requests: "circle"
    # (trusted circles) and/or "pool" (the open pool). None is a peer from
    # before audiences existed, which serves both. Unknown names are ignored,
    # so naming only audiences this coordinator does not know serves no one.
    audiences: list[str] | None = Field(default=None, max_length=8)


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
    # The peer's own concurrency cap (peers.max_concurrent_jobs). None keeps
    # whatever the row holds; 0 takes the peer out of new-job candidacy
    # without disconnecting it. Capped on the wire so a peer cannot claim an
    # absurd number to game the load term.
    max_concurrent_jobs: int | None = Field(default=None, ge=0, le=64)


# ---- Job dispatch (m3) ----------------------------------------------- #
#
# Privacy invariant (manifesto §10.3, memory 42a7e671):
#   - JobAssignedMessage MUST NOT carry member_id, source_conversation_id,
#     or any identifier that links the prompt back to its requester. The
#     coordinator strips these before sending. CI lint enforces this on
#     peer-bound code paths.
#   - Peer-side: prompt + tokens MUST NOT be written to disk, logs, or
#     telemetry. In-memory only.


# Hard ceiling on tokens a single job may request OR a peer may claim to have
# served. Bounds requester-side resource exhaustion (A3) and caps the headline
# number a peer can self-report toward credits (A2). This is a coarse guard, not
# verification: a peer can still over-report up to this ceiling for a short job.
# Real anti-fraud (sampled re-execution + reputation) is tracked separately.
MAX_OUTPUT_TOKENS = 4096


class SamplingParams(BaseModel):
    """Inference sampling parameters. Forward-compatible — add fields here
    rather than to JobAssignedMessage so older peers ignore unknown ones."""

    max_tokens: int = Field(default=512, ge=1, le=MAX_OUTPUT_TOKENS)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float | None = 0.95
    top_k: int | None = None
    repeat_penalty: float | None = None
    stop_sequences: list[str] | None = None
    seed: int | None = None


class ChatMessage(BaseModel):
    """One conversation turn forwarded verbatim to the serving peer.

    Contains ONLY role + content — no turn id, no timestamp, no requester
    metadata. The same blindness invariant as `prompt`: the peer learns
    the conversation text but nothing about who sent it."""

    role: str
    content: str


class JobAssignedMessage(BaseModel):
    """Server → Peer: 'run inference on this prompt and stream tokens back.'

    Carries ONLY the routing-blind subset: job id, model slug, prompt,
    optional multi-turn messages, and sampling params. No member_id, no
    conversation id, no anything that could identify the requester.

    Fields:
      prompt   — single-turn text (always present for backward compat with
                 peers that don't support messages).
      messages — OPTIONAL ordered list of conversation turns (role + content
                 only). When present the peer should prefer messages over
                 prompt for context. MUST carry ONLY conversation turns —
                 NEVER member_id / source_conversation_id / any requester
                 identifier. Same blindness invariant as prompt."""

    type: Literal["job_assigned"] = "job_assigned"
    job_id: uuid.UUID
    model_slug: str  # e.g. "llama-3.2-3b-instruct-q4_k_m"
    prompt: str
    messages: list[ChatMessage] | None = None
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
