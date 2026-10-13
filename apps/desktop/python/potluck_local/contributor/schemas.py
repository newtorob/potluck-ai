"""Wire-format schemas - peer side.

Mirrors services/coordinator/coordinator/schemas/peer.py. Keep these two
in sync until we extract a shared library. Any new field added on the
coordinator side must be added here too.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    # The audiences from our attestation that the coordinator enforces.
    # Absent from a coordinator that predates audiences.
    audiences: list[str] | None = None
    # Direct circle links. Both are sent only by a coordinator that has links
    # switched on, and both are soft: a value of the wrong kind reads as absent,
    # because an accept must never fail over them.
    #   mesh_node_id: the mesh node this attestation was bound to (an echo).
    #   circle_links: true when the coordinator brokers circle links. Absent or
    #                 anything but true means "not supported": no listener.
    mesh_node_id: uuid.UUID | None = None
    circle_links: bool | None = None

    @field_validator("mesh_node_id", mode="before")
    @classmethod
    def _lenient_mesh_node_id(cls, v: Any) -> uuid.UUID | None:
        if isinstance(v, uuid.UUID):
            return v
        try:
            return uuid.UUID(v) if isinstance(v, str) else None
        except ValueError:
            return None

    @field_validator("circle_links", mode="before")
    @classmethod
    def _lenient_circle_links(cls, v: Any) -> bool | None:
        return v if isinstance(v, bool) else None


class RejectMessage(BaseModel):
    type: Literal["reject"]
    reason: str


# ---- Peer → Server ---------------------------------------------------- #


class CapabilityEntry(BaseModel):
    """One model this peer can serve. Mirrors coordinator/schemas/peer.py.

    ``tokens_per_second`` is measured generation speed for this model on this
    machine; ``prompt_tokens_per_second`` is measured prompt-processing speed.
    Both are None until the sidecar has benchmarked the model. The
    coordinator uses them for scoring and for the first-token deadline
    instead of guessing from the hardware tier.
    """

    model_slug: str
    quantization: str = "unknown"
    tokens_per_second: float | None = None
    prompt_tokens_per_second: float | None = None


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
    # Capabilities. Empty list = stub-echo peer that doesn't
    # serve real inference; non-empty = list of models the coordinator can
    # route here.
    capabilities: list[CapabilityEntry] = Field(default_factory=list)
    # Who this peer agrees to serve beyond its owner's own requests: "circle"
    # and/or "pool". None serves both (older peers do not send this field).
    audiences: list[str] | None = None
    # Direct circle links: the mesh node this machine's sidecar is (its /status
    # node_id) and the port the circle listener is bound to. Sent only while the
    # listener is up; left out of the message otherwise, so a coordinator that
    # predates direct links sees an unchanged message.
    mesh_node_id: uuid.UUID | None = None
    circle_port: int | None = Field(default=None, ge=1, le=65535)


class HeartbeatMessage(BaseModel):
    """Sent every `heartbeat_interval_s` seconds after the handshake.

    Coordinator upserts peer_heartbeats with these values. None fields are
    interpreted as "no measurement available" - reasonable when a peer
    has no GPU or hasn't probed yet.
    """

    type: Literal["heartbeat"] = "heartbeat"
    peer_id: uuid.UUID
    session_id: uuid.UUID
    timestamp: datetime
    current_jobs: int = 0
    gpu_utilization: float | None = None
    vram_available_gb: float | None = None
    # How many jobs this peer is willing to run at once. None keeps the
    # coordinator's current value (uncapped for a peer that never said).
    max_concurrent_jobs: int | None = None


# ---- Job dispatch ---------------------------------------------------- #
#
# Mirrors services/coordinator/coordinator/schemas/peer.py - keep in sync.
#
# Architectural invariant: this contributor process must not write prompt
# content or generated tokens to disk, logs, or telemetry beyond the
# minimal counters in JobCompleteMessage. CI lint enforces this; do not
# reach for logger.info("prompt=%s", prompt) under any circumstances.


class SamplingParams(BaseModel):
    """Inference sampling parameters. Forward-compatible - older peers
    ignore unknown fields."""

    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float | None = 0.95
    top_k: int | None = None
    repeat_penalty: float | None = None
    stop_sequences: list[str] | None = None
    seed: int | None = None


class ChatMessage(BaseModel):
    """One turn in a multi-turn conversation, on the contributor wire.

    Mirrors coordinator/schemas/peer.py - keep in sync. The field order
    (role, content) is stable; do not reorder without bumping the wire
    version. Extra fields are silently ignored (Pydantic default) so old
    contributors that receive an unknown field are unaffected.
    """

    role: str
    content: str


class JobAssignedMessage(BaseModel):
    """Server → Peer (incoming on this side). Contains the prompt + sampling;
    no member_id or conversation id by design - the coordinator strips
    them before forwarding.

    Multi-turn extension: `messages` carries the full conversation when the
    requesting scope permits it (circle: always; pool: only with explicit
    opt-in). `prompt` is always populated as the backward-compat fallback -
    an old contributor that doesn't know `messages` ignores the field and
    uses `prompt`, which is the last user message.
    """

    type: Literal["job_assigned"]
    job_id: uuid.UUID
    model_slug: str
    prompt: str
    messages: list[ChatMessage] | None = None
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
    """Peer → Server (outgoing on this side). Sent instead of JobComplete
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
