"""Model registry — loads the curated catalog and merges installation status.

The local catalog in potluck_local/data/models.json is the source of truth
for v0. Cloud sync is a future enhancement.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
from pathlib import Path

from potluck_local.utils.paths import models_dir as default_models_dir

logger = logging.getLogger(__name__)

_catalog: list[dict] | None = None


def _load_catalog() -> list[dict]:
    global _catalog
    if _catalog is not None:
        return _catalog

    ref = importlib.resources.files("potluck_local.data").joinpath("models.json")
    with ref.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    _catalog = data.get("models", [])
    logger.info("catalog_loaded count=%d", len(_catalog))
    return _catalog


def _installed_slugs(base_dir: Path) -> set[str]:
    if not base_dir.exists():
        return set()
    return {
        entry.name
        for entry in base_dir.iterdir()
        if entry.is_dir() and (entry / "model.gguf").exists()
    }


def _size_on_disk(base_dir: Path, slug: str) -> int | None:
    gguf = base_dir / slug / "model.gguf"
    try:
        return gguf.stat().st_size
    except OSError:
        return None


def list_models() -> list[dict]:
    """Return catalog entries merged with local installation status."""
    from potluck_local.services.model_downloader import get_downloader

    catalog = _load_catalog()
    base_dir = default_models_dir()
    installed = _installed_slugs(base_dir)
    downloader = get_downloader()

    result = []
    for entry in catalog:
        item = dict(entry)
        slug = item["slug"]
        item["installed"] = slug in installed
        if item["installed"]:
            size = _size_on_disk(base_dir, slug)
            if size is not None:
                item["size_on_disk"] = size

        progress = downloader.get_progress(slug)
        if progress is not None:
            item["download_progress"] = {
                "status": progress.status,
                "bytes_downloaded": progress.bytes_downloaded,
                "bytes_total": progress.bytes_total,
                "speed_bps": progress.speed_bps,
                "eta_seconds": progress.eta_seconds,
            }
        result.append(item)

    return result


def get_model(slug: str) -> dict | None:
    """Return a single catalog entry with installation status, or None."""
    for item in list_models():
        if item["slug"] == slug:
            return item
    return None


def get_manifest(slug: str) -> dict | None:
    """Return the raw catalog entry for a slug, or None if not found."""
    for entry in _load_catalog():
        if entry["slug"] == slug:
            return dict(entry)
    return None
