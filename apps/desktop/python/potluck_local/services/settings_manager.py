"""User settings manager.

Reads and writes ~/.potluck/config.json using Pydantic models.
Full implementation per guide §4.4 and §9.9 — small, self-contained, no heavy deps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
            "reference",
        ]
    )


class VaultSettings(BaseModel):
    """Vault ingestion configuration.

    A vault is any folder of markdown notes the user wants ingested into the
    memory store as 'reference' memories. Per-heading chunks become graph
    nodes, retrievable by connected agents via the memory_search MCP tool.
    """

    folder: str | None = None
    last_scan_at: str | None = None
    file_count: int = 0
    chunk_count: int = 0


class GatewaySettings(BaseModel):
    """OpenAI/Ollama-compatible gateway config.

    When enabled with an api_key, external tools (Cursor, Continue, etc.) can
    point at http://127.0.0.1:8321/v1 using the api_key as their OpenAI key. The
    key gates ONLY the /v1 gateway surface, never the rest of the sidecar. The
    middleware reads these live, so toggling on/rotating the key takes effect
    without a restart.
    """

    enabled: bool = False
    api_key: str = ""
    # Open-pool routing: when a gateway request asks for a model that is neither
    # local nor on a household peer, allow routing through the coordinator to the
    # open pool (other members' machines, blinded + single-turn). Off by default
    # — household routing serves your own machines automatically, but reaching
    # strangers is the widest scope and stays an explicit opt-in.
    pool_routing: bool = False

    @model_validator(mode="after")
    def _require_key_when_enabled(self) -> "GatewaySettings":
        # The gateway cannot be enabled without a key; normalise an invalid
        # persisted state (the middleware also guards on both, this keeps disk clean).
        if self.enabled and not self.api_key:
            self.enabled = False
        # Pool routing is meaningless without the gateway itself enabled.
        if self.pool_routing and not self.enabled:
            self.pool_routing = False
        return self


class PotluckSettings(BaseModel):
    """Top-level settings model persisted to ~/.potluck/config.json."""

    runtime: RuntimeSettings = RuntimeSettings()
    privacy: PrivacySettings = PrivacySettings()
    network: NetworkSettings = NetworkSettings()
    app: AppSettings = AppSettings()
    memory: MemorySettings = MemorySettings()
    vault: VaultSettings = VaultSettings()
    gateway: GatewaySettings = GatewaySettings()
    models_path: str = str(Path.home() / ".potluck" / "models")
    active_model_id: str | None = None
    active_project: str | None = None


_CONFIG_PATH = Path.home() / ".potluck" / "config.json"


_settings_cache: dict[str, tuple[float, PotluckSettings]] = {}


def get_settings(config_path: Path = _CONFIG_PATH) -> PotluckSettings:
    """Load settings from disk, returning defaults if the file is absent.

    Cached by file mtime so hot callers (e.g. the gateway middleware on every
    /v1 request) don't re-read and re-parse the file each time. A save changes
    the mtime, so live updates are still picked up. The config_path parameter
    exists for testability (pass a tmp_path).
    """
    if config_path.exists():
        try:
            mtime = config_path.stat().st_mtime
            key = str(config_path)
            cached = _settings_cache.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            data = json.loads(config_path.read_text(encoding="utf-8"))
            settings = PotluckSettings(**data)
            _settings_cache[key] = (mtime, settings)
            return settings
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
    # Keep the mtime cache coherent so the next get_settings reflects this write.
    try:
        _settings_cache[str(config_path)] = (config_path.stat().st_mtime, settings)
    except OSError:
        _settings_cache.pop(str(config_path), None)
