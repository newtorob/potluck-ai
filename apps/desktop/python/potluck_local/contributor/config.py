"""Contributor sidecar configuration.

All values overridable via environment variables (POTLUCK_*). The defaults
are dev-friendly: connect to a local coordinator on port 8400.
"""
from __future__ import annotations

import functools
import uuid
from pathlib import Path

import platformdirs
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_key_dir() -> Path:
    """Where the per-peer Ed25519 key lives.

    Uses platformdirs so the path is conventional per OS:
      macOS:    ~/Library/Application Support/Potluck/contributor/
      Linux:    ~/.local/share/Potluck/contributor/   (XDG_DATA_HOME)
      Windows:  %LOCALAPPDATA%/Potluck/contributor/
    """
    base = platformdirs.user_data_dir(appname="Potluck", appauthor=False)
    return Path(base) / "contributor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POTLUCK_",
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

    # WebSocket URL for the coordinator's /peers/connect endpoint.
    coordinator_url: str = "ws://localhost:8400/peers/connect"

    # Which member this peer belongs to. Stub-UUID for m1 dev; real
    # membership integration lands in v0.5 (see roadmap memory 6d50b79d).
    member_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    # SHA256 of the contributor binary, hex-encoded. The coordinator
    # rejects connections whose hash isn't in `contributor_releases`. In
    # dev: produced by `scripts/sign-contributor-release.py emit-sql ...`
    # and seeded via psql. In prod: computed at install-time from the
    # signed release artifact.
    binary_hash: str = ""

    # Where to load + persist the per-peer Ed25519 key.
    key_dir: Path = _default_key_dir()

    # ---- Inference (m3) ---- #
    #
    # Set inference_enabled=True + model_slug + model_path on peers that
    # actually have a GPU + GGUF and should serve real jobs. When False
    # (the default), the peer accepts JobAssignedMessages but responds
    # with a stub-echo handler. Useful for protocol smoke testing without
    # needing a model on every machine.
    inference_enabled: bool = False
    model_slug: str | None = None  # e.g. "qwen-2.5-3b-instruct-q4"
    model_path: Path | None = None  # absolute path to the GGUF file
    n_gpu_layers: int = -1  # -1 = offload all (Metal/CUDA); 0 = CPU only
    n_ctx: int = 4096


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
