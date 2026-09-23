"""User settings manager.

Reads and writes ~/.potluck/config.json using Pydantic models.
Full implementation per guide §4.4 and §9.9 — small, self-contained, no heavy deps.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RuntimeSettings(BaseModel):
    # Only a successfully loaded local GGUF is eligible for startup restore.
    # Separate from active_model_id (the coding agent's model preference).
    startup_model_id: str | None = None
    # 0 = auto-tune n_ctx per model + available RAM (default). A value > 0 pins
    # n_ctx, capped at the loaded model's maximum context window.
    context_size: int = 0
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
    contributor_mode: bool = False       # The Contribute compute toggle
    contributor_schedule: dict[str, object] | None = None
    max_gpu_percent: int = 80
    # Coordinator jobs this machine serves at once. Sent in every heartbeat
    # and enforced locally by the contributor loop. The runtime serialises
    # inference on one model, so 1 is the honest default.
    max_concurrent_jobs: int = Field(default=1, ge=0, le=64)
    # HTTP base of the coordinator this node talks to (e.g. "http://127.0.0.1:8400").
    # Synced from the frontend's connection setting so the sidecar can reach the
    # open pool for gateway routing. Empty = not configured (pool unavailable).
    coordinator_url: str = ""


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
    # Multi-turn for pool contributors: when True, send the full messages array
    # to pool contributors instead of only the last user message.
    # Default OFF — pool contributors are strangers; sending conversation history
    # to strangers is a privacy widening that requires explicit user opt-in.
    # Circle-scope jobs always send the full messages array regardless of this
    # setting (trusted-member invariant). Key: gateway.pool_multiturn.
    pool_multiturn: bool = False

    @model_validator(mode="after")
    def _require_key_when_enabled(self) -> "GatewaySettings":
        # The gateway cannot be enabled without a key; normalise an invalid
        # persisted state (the middleware also guards on both, this keeps disk clean).
        if self.enabled and not self.api_key:
            self.enabled = False
        # Pool routing is meaningless without the gateway itself enabled.
        if self.pool_routing and not self.enabled:
            self.pool_routing = False
        # pool_multiturn is meaningless without pool_routing.
        if self.pool_multiturn and not self.pool_routing:
            self.pool_multiturn = False
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

    # Generic string→string bag for frontend UI preferences (theme, routing
    # scope, sidebar state, etc.). The frontend owns the key namespace; the
    # backend treats this as an opaque dict and deep-merges patches so writing
    # one key never silently drops another.
    ui: dict[str, str] = Field(default_factory=dict)


def _default_config_path() -> Path:
    """~/.potluck/config.json, honouring POTLUCK_DATA_DIR when set.

    Resolved lazily (at call time, not import time) via utils.paths.config_path
    — the same POTLUCK_DATA_DIR-aware helper every other on-disk store
    (memory.db, models/, logs/) routes through. Settings used to hardcode
    Path.home() / ".potluck" as a *default argument value*, which is baked in
    at function-definition time; even a caller that set POTLUCK_DATA_DIR
    before importing this module would still get the real home directory's
    config.json (and its gateway.api_key) mixed into a supposedly-isolated
    override instance. Computing it fresh on every call closes that gap.
    """
    from potluck_local.utils.paths import config_path as _config_path_helper
    return _config_path_helper()


_settings_cache: dict[str, tuple[float, PotluckSettings]] = {}


def get_settings(config_path: Path | None = None) -> PotluckSettings:
    """Load settings from disk, returning defaults if the file is absent.

    Cached by file mtime so hot callers (e.g. the gateway middleware on every
    /v1 request) don't re-read and re-parse the file each time. A save changes
    the mtime, so live updates are still picked up. The config_path parameter
    exists for testability (pass a tmp_path); when omitted it resolves via
    _default_config_path(), which honours POTLUCK_DATA_DIR.
    """
    if config_path is None:
        config_path = _default_config_path()
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
    config_path: Path | None = None,
) -> None:
    """Persist settings to disk, creating the directory if needed.

    config.json holds secrets in cleartext (gateway.api_key), so the file is
    chmod'd 0600 and its parent dir 0700 on every save — idempotent, cheap,
    and closes the window where a freshly-created file was briefly
    world-readable before permissions were applied. os.chmod is a partial
    no-op on Windows (no POSIX mode bits); best-effort there, never fatal.

    config_path defaults to _default_config_path() (POTLUCK_DATA_DIR-aware)
    when omitted, same as get_settings().
    """
    if config_path is None:
        config_path = _default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_path.parent, 0o700)
    except OSError:
        pass
    # Publish an entire private file atomically. Readers in the CLI/app must
    # never observe a truncated document or a briefly world-readable key.
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(settings.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    # Keep the mtime cache coherent so the next get_settings reflects this write.
    try:
        _settings_cache[str(config_path)] = (config_path.stat().st_mtime, settings)
    except OSError:
        _settings_cache.pop(str(config_path), None)
