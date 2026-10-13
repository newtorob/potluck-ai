from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coordinator.db.base import Base


class Peer(Base):
    __tablename__ = "peers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    member_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    public_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    hostname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hardware_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    max_gpu_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("80")
    )
    # Optional per-peer concurrency cap: the most in-flight jobs this peer
    # will be dispatched at once. NULL (the default for every existing row,
    # and for any peer that never sets it) means "uncapped" - the coordinator
    # falls back to score-based load balancing only. Lives on `peers` (not `peer_heartbeats`) because it's a
    # slow-moving, largely-static capacity fact about the machine - the same
    # shape as `max_gpu_percent` above - rather than per-beat dynamic load
    # (that's `peer_heartbeats.current_jobs` / the live in-flight counter).
    # See migration 0010_peer_max_concurrent_jobs.
    max_concurrent_jobs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )


class PeerCapability(Base):
    __tablename__ = "peer_capabilities"

    peer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_slug: Mapped[str] = mapped_column(String(128), primary_key=True)
    quantization: Mapped[str] = mapped_column(String(16), nullable=False)
    tokens_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Prompt-processing speed the peer measured for this model; the
    # first-token deadline prefers it over tokens_per_second (generation),
    # which is the wrong rate for "how long until the first token". See
    # migration 0012.
    prompt_tokens_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PeerHeartbeat(Base):
    __tablename__ = "peer_heartbeats"

    peer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("peers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_beat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    gpu_utilization: Mapped[float | None] = mapped_column(Float, nullable=True)
    vram_available_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_jobs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    reputation: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.5")
    )
    binary_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    client_version: Mapped[str] = mapped_column(String(32), nullable=False)
