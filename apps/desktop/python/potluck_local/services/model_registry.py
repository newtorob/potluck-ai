"""Model registry - loads the curated catalog and merges installation status.

The bundled catalog in potluck_local/data/models.json is the offline floor -
always present, ships inside the packaged app. services.catalog_feed
periodically fetches a cloud copy in the background and caches it to disk
(utils.paths.catalog_cache_path()); _load_catalog() below picks whichever of
the two is FRESHER and structurally valid, never blocking a request on the
network itself.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
from pathlib import Path
from typing import Any

from potluck_local.utils.paths import models_dir as default_models_dir

logger = logging.getLogger(__name__)

_catalog: list[dict] | None = None


def _load_bundled_catalog() -> dict[str, Any]:
    ref = importlib.resources.files("potluck_local.data").joinpath("models.json")
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_catalog() -> list[dict]:
    """Return the freshest valid catalog's model list.

    Bundled is always the fallback. The cached remote feed (written by
    services.catalog_feed's background refresh) is preferred only when it
    parses and its schema_version >= bundled's and its updated_at >=
    bundled's (bundled ships with no updated_at, so any remote timestamp
    beats it once the schema version is compatible). Any error reading
    either source falls back toward bundled; this never raises and never
    returns an empty list when bundled has entries.
    """
    global _catalog
    if _catalog is not None:
        return _catalog

    bundled = _load_bundled_catalog()
    chosen = bundled.get("models", [])

    try:
        from potluck_local.services.catalog_feed import read_cached_catalog

        cached = read_cached_catalog()
    except Exception:  # noqa: BLE001 - cache read must never block catalog load
        cached = None

    if cached is not None:
        schema_ok = cached.get("schema_version", 0) >= bundled.get("schema_version", 0)
        fresh_ok = str(cached.get("updated_at", "")) >= str(bundled.get("updated_at", ""))
        cached_models = cached.get("models")
        if schema_ok and fresh_ok and cached_models:
            chosen = cached_models

    _catalog = chosen
    logger.info("catalog_loaded count=%d", len(_catalog))
    return _catalog


def invalidate_cache() -> None:
    """Drop the in-memory merged catalog so the next _load_catalog() call
    re-evaluates bundled vs cached-remote. Called by catalog_feed after a
    successful background refresh lands a new file on disk."""
    global _catalog
    _catalog = None


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

        # Surface "this catalog entry supersedes something you have installed"
        # without synthesizing an entry for a model that's been removed from
        # the catalog - only the still-installed old slugs are listed.
        upgrade_from = [s for s in entry.get("supersedes", []) if s in installed]
        if upgrade_from:
            item["upgrade_from"] = upgrade_from

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
