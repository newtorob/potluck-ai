"""Contributor sidecar configuration.

All values overridable via environment variables (POTLUCK_*). The defaults
are dev-friendly: connect to a local coordinator on port 8400.
"""
from __future__ import annotations

import functools
import uuid
from pathlib import Path

import platformdirs
from pydantic import Field
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

    # Optional coordinator token. When the coordinator enforces
    # POTLUCK_COORDINATOR_TOKEN, the contributor must present it on the WS
    # handshake (X-Potluck-Coordinator-Token header). Unset by default —
    # dev/household coordinators run open.
    coordinator_token: str | None = None

    # Which member this peer belongs to. Stub-UUID for m1 dev; real
    # membership integration lands in v0.5 (see roadmap memory 6d50b79d).
    member_id: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

    # SHA256 of the contributor build, hex-encoded. Empty means "resolve it":
    # see contributor/release.py (release file next to the frozen executable,
    # else the dev placeholder). The coordinator records whatever is sent and
    # only rejects unknown hashes when its require_registered_release flag is
    # on. In dev: `scripts/dev/sign-contributor-release.py emit-sql ...`
    # seeds a matching contributor_releases row.
    binary_hash: str = ""

    # Jobs this peer runs at once. Sent in every heartbeat; the loop also
    # declines a job locally once this many are in flight. 1 matches the
    # sidecar runtime, which serialises inference on one model anyway.
    max_concurrent_jobs: int = Field(default=1, ge=0, le=64)

    # Where to load + persist the per-peer Ed25519 key.
    key_dir: Path = _default_key_dir()

    # ---- Inference (m3) ---- #
    #
    # Set inference_enabled=True on peers that should serve real jobs.
    # When False (the default), the peer accepts JobAssignedMessages but
    # responds with a stub-echo handler. Useful for protocol smoke
    # testing without needing a model on every machine.
    #
    # Two backends:
    #   - "llama-cpp" (default): in-process via llama-cpp-python. Best for
    #     NVIDIA (CUDA wheels) and Apple Silicon (Metal). Requires
    #     model_path pointing at a GGUF file.
    #   - "ollama": HTTP roundtrip to a local Ollama daemon. Best for AMD
    #     and Intel GPUs on Windows where llama-cpp-python's prebuilt
    #     wheels don't cover GPU acceleration. model_slug must be the
    #     Ollama-native model name (e.g. "qwen2.5:3b-instruct-q4_K_M").
    inference_enabled: bool = False
    inference_backend: str = "llama-cpp"  # "llama-cpp" or "ollama"
    model_slug: str | None = None  # what the coordinator routes by

    # llama-cpp-only: path to the GGUF file + GPU layer config.
    model_path: Path | None = None
    n_gpu_layers: int = -1  # -1 = offload all (Metal/CUDA); 0 = CPU only

    # Ollama-only: where the local daemon listens. MUST be loopback —
    # _validate_loopback in inference.py refuses any other host as a
    # privacy-invariant guard.
    ollama_url: str = "http://127.0.0.1:11434"

    # Shared: context window. llama-cpp passes to Llama(n_ctx=...);
    # Ollama passes via options.num_ctx.
    n_ctx: int = 4096


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
