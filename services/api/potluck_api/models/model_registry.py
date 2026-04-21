from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from potluck_api.db.base import Base

# NOTE: Class is named ModelRegistryEntry, NOT Model, to avoid shadowing
# Pydantic's BaseModel in any shared namespace.


class ModelRegistryEntry(Base):
    __tablename__ = "models_registry"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_models_registry_slug_version"),
        CheckConstraint(
            "tier IN ('small', 'medium', 'large')",
            name="ck_models_registry_tier",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # External identifier, e.g. "llama-3.1-8b-instruct-q5". Unique alone for
    # the "latest" lookup; versioned uniqueness enforced by the composite UC above.
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    min_ram_gb: Mapped[float] = mapped_column(
        Numeric(4, 1), nullable=False, server_default=text("0")
    )
    min_vram_gb: Mapped[float] = mapped_column(
        Numeric(4, 1), nullable=False, server_default=text("0")
    )
    # "small" | "medium" | "large"
    tier: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'medium'")
    )
    download_url: Mapped[str] = mapped_column(String(512), nullable=False)
    manifest_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_featured: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # v1 seam: model will be served by contributor peers rather than central infra.
    is_hosted_by_peers: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
