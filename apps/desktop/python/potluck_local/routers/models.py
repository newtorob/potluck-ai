"""Model management router.

GET    /models                             — list available/installed models
POST   /models/{model_id}/install          — start download (background task)
DELETE /models/{model_id}                  — delete installed model
POST   /models/{model_id}/load             — load model into memory
GET    /models/{model_id}/download-progress — poll download progress

All endpoints are stubbed pending services/model_downloader.py and
services/runtime.py (guide §4.2, §4.3).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("")
async def list_models() -> list[dict[str, object]]:
    """Return available models merged with local install status.

    TODO: Fetch curated list from cloud API, merge with local models_dir
          scan, and annotate each entry with installed/downloading status.
          See guide §4.2 and services/model_registry.py.
    """
    return []


@router.post("/{model_id}/install", status_code=202)
async def install_model(model_id: str) -> dict[str, object]:
    """Start downloading a model from the registry.

    TODO: Fetch manifest from cloud API, enqueue background download via
          services/model_downloader.ModelDownloader (guide §4.2).
    """
    raise HTTPException(status_code=501, detail="Not implemented")


@router.delete("/{model_id}", status_code=200)
async def delete_model(model_id: str) -> dict[str, object]:
    """Delete an installed model from disk.

    TODO: Unload from runtime if active, then shutil.rmtree the model dir
          (guide §4.2).
    """
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/{model_id}/load", status_code=200)
async def load_model(model_id: str) -> dict[str, object]:
    """Load an installed model into memory for inference.

    TODO: Resolve model path, read runtime settings, call
          services/runtime.LLMRuntime.load_model() (guide §4.2, §4.4).
    """
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/{model_id}/download-progress")
async def download_progress(model_id: str) -> dict[str, object]:
    """Return current download progress for a model.

    TODO: Delegate to services/model_downloader.ModelDownloader.get_progress()
          (guide §4.2).
    """
    raise HTTPException(status_code=404, detail="No active download for this model")
