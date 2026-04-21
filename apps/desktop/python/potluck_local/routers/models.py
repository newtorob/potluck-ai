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

import logging
import shutil

from fastapi import APIRouter, BackgroundTasks, HTTPException

from potluck_local.services import model_registry
from potluck_local.services.model_downloader import get_downloader
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings
from potluck_local.utils.paths import models_dir

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_models() -> list[dict]:
    try:
        return model_registry.list_models()
    except Exception:
        logger.exception("model_registry.list_models failed")
        return []


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


@router.post("/{slug}/load", status_code=200)
async def load_model(slug: str) -> dict:
    model_path = models_dir() / slug / "model.gguf"
    if not model_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Model '{slug}' is not installed. POST /models/{slug}/install first.",
        )

    settings = get_settings()
    runtime = get_runtime()

    logger.info("load_model_start slug=%s", slug)
    result = runtime.load_model(
        str(model_path),
        n_ctx=settings.runtime.context_size,
        n_gpu_layers=settings.runtime.gpu_layers,
        n_threads=settings.runtime.threads or None,
    )
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
