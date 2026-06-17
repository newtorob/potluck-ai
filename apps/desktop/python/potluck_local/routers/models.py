"""Model management router.

GET    /models                              — list available/installed models
GET    /models/{slug}/manifest              — fetch a single model's manifest
POST   /models/{slug}/install               — start download (background task)
DELETE /models/{slug}                       — delete installed model
POST   /models/{slug}/load                  — load model into runtime
POST   /models/{slug}/unload               — unload model from runtime
GET    /models/{slug}/download-progress     — poll download progress
POST   /models/{slug}/cancel-download      — cancel an active download
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from potluck_local.services import model_registry
from potluck_local.services.model_downloader import get_downloader
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings
from potluck_local.utils.crypto import sha256_file
from potluck_local.utils.paths import models_dir

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_models() -> list[dict]:
    try:
        models = model_registry.list_models()
    except Exception:
        logger.exception("model_registry.list_models failed")
        return []
    # Merge in each model's last benchmark (tokens/sec + TTFT), if it has been run.
    benches = get_runtime().benchmarks()
    for m in models:
        b = benches.get(m.get("slug"))
        if b:
            m["benchmark"] = b
    return models


@router.post("/benchmark")
async def benchmark_loaded_model() -> dict:
    """Benchmark the currently loaded model: tokens/sec + time-to-first-token."""
    runtime = get_runtime()
    if not runtime.is_loaded():
        raise HTTPException(status_code=503, detail="No model loaded to benchmark.")
    try:
        return await runtime.benchmark()
    except Exception as exc:  # noqa: BLE001
        logger.exception("benchmark_failed")
        raise HTTPException(status_code=500, detail=f"benchmark failed: {type(exc).__name__}")


@router.get("/{slug}/manifest")
async def get_manifest(slug: str) -> dict:
    manifest = model_registry.get_manifest(slug)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No manifest found for '{slug}'.")
    return manifest


@router.post("/{slug}/install", status_code=202)
async def install_model(slug: str, background_tasks: BackgroundTasks) -> dict:
    manifest = model_registry.get_manifest(slug)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Model '{slug}' not found in registry.")

    # Integrity gate: a catalog entry must carry a sha256 so the download is
    # verified. The downloader skips verification on an empty hash (a utility
    # affordance for direct/test calls); refuse it here so a catalog regression
    # can never install an unverified model.
    if not str(manifest.get("sha256", "")).strip():
        raise HTTPException(
            status_code=500,
            detail=f"Catalog entry for '{slug}' has no sha256; refusing to install unverified.",
        )

    model_path = models_dir() / slug / "model.gguf"
    if model_path.exists():
        return {"status": "already_installed"}

    downloader = get_downloader()
    existing_progress = downloader.get_progress(slug)
    if existing_progress is not None and existing_progress.status == "downloading":
        return {"status": "already_downloading"}

    background_tasks.add_task(
        downloader.download_model,
        model_id=slug,
        url=manifest["download_url"],
        expected_sha256=manifest.get("sha256", ""),
        expected_size=manifest["size_bytes"],
    )

    logger.info("install_started slug=%s", slug)
    return {"status": "downloading", "slug": slug}


@router.delete("/{slug}", status_code=200)
async def delete_model(slug: str) -> dict:
    model_dir = models_dir() / slug
    if not model_dir.exists():
        raise HTTPException(status_code=404, detail=f"Model '{slug}' is not installed.")

    runtime = get_runtime()
    if runtime.is_loaded() and runtime.current_model_id() == slug:
        runtime.unload_model()

    shutil.rmtree(model_dir)
    logger.info("model_deleted slug=%s", slug)
    return {"status": "deleted"}


class LoadModelRequest(BaseModel):
    """Optional body for POST /models/{slug}/load.

    rpc_servers: when set, the model loads with one or more remote
    llama.cpp RPC servers participating as ggml-backend devices. This is
    Big Model Mode — used to run a model too big to fit on this machine
    alone, by spreading weights/KV/compute across household peers that
    are running `rpc-server`. Endpoints look like "100.64.0.6:50052".
    Requires the Potluck-patched llama-cpp-python wheel.
    """

    rpc_servers: list[str] | None = Field(default=None)


@router.post("/{slug}/load", status_code=200)
async def load_model(slug: str, body: LoadModelRequest | None = None) -> dict:
    model_path = models_dir() / slug / "model.gguf"
    if not model_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Model '{slug}' is not installed. POST /models/{slug}/install first.",
        )

    # Opt-in integrity re-check against the sha256 recorded at download time.
    # Off by default: a full re-hash of a multi-GB model adds seconds to load.
    # Enable with POTLUCK_VERIFY_MODEL_ON_LOAD=1 to catch post-download tampering
    # or a manually-dropped file.
    if os.environ.get("POTLUCK_VERIFY_MODEL_ON_LOAD") == "1":
        sidecar = models_dir() / slug / "model.gguf.sha256"
        if sidecar.exists():
            expected = sidecar.read_text().strip().lower()
            loop = asyncio.get_running_loop()
            actual = await loop.run_in_executor(None, lambda: sha256_file(model_path))
            if expected and actual != expected:
                logger.error("load_verify_mismatch slug=%s", slug)
                raise HTTPException(
                    status_code=500,
                    detail=f"Model '{slug}' failed its integrity check (sha256 mismatch); refusing to load.",
                )
            logger.info("load_verify_ok slug=%s", slug)

    settings = get_settings()
    runtime = get_runtime()

    rpc_servers = body.rpc_servers if body and body.rpc_servers else None

    logger.info(
        "load_model_start slug=%s rpc_servers=%s",
        slug,
        ",".join(rpc_servers) if rpc_servers else "(none)",
    )
    try:
        result = runtime.load_model(
            str(model_path),
            n_ctx=settings.runtime.context_size,
            n_gpu_layers=settings.runtime.gpu_layers,
            n_threads=settings.runtime.threads or None,
            rpc_servers=rpc_servers,
        )
    except TypeError as exc:
        # Stock (non-Potluck-patched) llama-cpp-python doesn't accept the
        # rpc_servers kwarg. Surface a clean error so the frontend can
        # tell the user to upgrade their wheel.
        if rpc_servers and "rpc_servers" in str(exc):
            raise HTTPException(
                status_code=501,
                detail=(
                    "Big Model Mode requires the Potluck-patched "
                    "llama-cpp-python wheel. This sidecar appears to be running "
                    "stock llama-cpp-python. Install the patched wheel from "
                    "~/Documents/potluck-parallel/llama-cpp-python or rebuild "
                    "with CMAKE_ARGS='-DGGML_RPC=ON'."
                ),
            )
        raise
    logger.info("load_model_complete slug=%s load_time=%.2f", slug, result.get("load_time_seconds", 0))
    return result


@router.post("/{slug}/unload", status_code=200)
async def unload_model(slug: str) -> dict:
    runtime = get_runtime()
    if runtime.is_loaded() and runtime.current_model_id() == slug:
        runtime.unload_model()
        logger.info("model_unloaded slug=%s", slug)
    return {"status": "unloaded"}


@router.get("/{slug}/download-progress")
async def download_progress(slug: str) -> dict:
    downloader = get_downloader()
    progress = downloader.get_progress(slug)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"No download record for '{slug}'.")
    # DownloadProgress is a dataclass; return as dict for serialisation.
    if hasattr(progress, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(progress)
    return dict(progress)


@router.post("/{slug}/cancel-download", status_code=200)
async def cancel_download(slug: str) -> dict:
    downloader = get_downloader()
    downloader.cancel_download(slug)
    logger.info("download_cancelled slug=%s", slug)
    return {"status": "cancelled"}
