"""User settings manager.

Reads and writes ~/.potluck/config.json using Pydantic models.
Full implementation per guide §4.4 and §9.9 — small, self-contained, no heavy deps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class RuntimeSettings(BaseModel):
    context_size: int = 4096
    gpu_layers: int = -1   # -1 = offload all layers to GPU automatically
    threads: int = 0       # 0 = auto-detect
    temperature: float = 0.7
    max_tokens: int = 2048


class PrivacySettings(BaseModel):
    chat_history_enabled: bool = False
    telemetry_enabled: bool = False
    crash_reports_enabled: bool = True
    memory_enabled: bool = True   # master kill-switch; checked alongside memory.enabled


class NetworkSettings(BaseModel):
    contributor_mode: bool = False       # Disabled in v0; reserved for v1
    contributor_schedule: dict[str, object] | None = None
    max_gpu_percent: int = 80


class AppSettings(BaseModel):
    theme: str = "system"   # "system" | "light" | "dark"
    font_size: int = 14


class MemorySettings(BaseModel):
    """Behavioural tuning knobs for the memory subsystem.

    The master on/off switch is privacy.memory_enabled. These fields control
    how memory behaves when it is active.
    """

    enabled: bool = True
    auto_extract: bool = True
    auto_retrieve: bool = True
    extraction_frequency: Literal[
        "end_of_conversation", "every_turn", "manual"
    ] = "end_of_conversation"
    max_memories: int = 1000                    # soft cap; retention job trims
    max_retrieval: int = 20                     # memories injected per turn
    min_confidence_for_retrieval: float = 0.5
    retention_days: int = 365
    categories_enabled: list[str] = Field(
        default_factory=lambda: [
            "preference",
            "fact",
            "decision",
            "project",
            "person",
            "instruction",
        ]
    )


class PotluckSettings(BaseModel):
    """Top-level settings model persisted to ~/.potluck/config.json."""

    runtime: RuntimeSettings = RuntimeSettings()
    privacy: PrivacySettings = PrivacySettings()
    network: NetworkSettings = NetworkSettings()
    app: AppSettings = AppSettings()
    memory: MemorySettings = MemorySettings()
    models_path: str = str(Path.home() / ".potluck" / "models")
    active_model_id: str | None = None
    active_project: str | None = None


_CONFIG_PATH = Path.home() / ".potluck" / "config.json"


def get_settings(config_path: Path = _CONFIG_PATH) -> PotluckSettings:
    """Load settings from disk, returning defaults if the file is absent.

    The config_path parameter exists for testability (pass a tmp_path).
    """
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return PotluckSettings(**data)
        except Exception:
            # Corrupted config — fall back to defaults rather than crashing.
            return PotluckSettings()
    return PotluckSettings()


def save_settings(
    settings: PotluckSettings,
    config_path: Path = _CONFIG_PATH,
) -> None:
    """Persist settings to disk, creating the directory if needed."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
