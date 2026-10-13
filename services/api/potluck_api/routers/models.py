from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])

# Bundled alongside this package -- kept in sync with the desktop sidecar's
# apps/desktop/python/potluck_local/data/models.json copy by
# scripts/check_model_updates.py --write (see that script's header for the
# sync mechanism). This file is what GET /models/catalog serves; the sidecar
# fetches it, merges it with its own bundled copy (its offline floor), and
# never trusts this endpoint alone.
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "models.json"

# Parsed-and-wrapped catalog, cached for the life of the process. The file
# only changes via a redeploy (a new image built after --write regenerates
# it), so there is nothing to invalidate at runtime -- unlike the sidecar's
# merge cache, which does need runtime invalidation because it also layers
# in a live network fetch.
_catalog_cache: dict[str, Any] | None = None


def _load_catalog_feed() -> dict[str, Any]:
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache

    with _CATALOG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    updated_at = data.get("updated_at")
    if not updated_at:
        mtime = _CATALOG_PATH.stat().st_mtime
        updated_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    _catalog_cache = {
        "schema_version": data.get("schema_version", 1),
        "updated_at": updated_at,
        "models": data.get("models", []),
    }
    logger.info("catalog_feed_loaded count=%d", len(_catalog_cache["models"]))
    return _catalog_cache


# ---------------------------------------------------------------------------
# GET /models/catalog
# ---------------------------------------------------------------------------


@router.get("/catalog")
async def get_catalog() -> dict[str, Any]:
    """Return the full model-catalog feed for the desktop sidecar to consume.

    The sidecar (not this endpoint's caller directly) owns downloads; it
    periodically fetches this, validates it, caches it to disk, and merges
    it against its bundled catalog -- see
    apps/desktop/python/potluck_local/services/catalog_feed.py. This route
    stays a plain, unauthenticated GET so it degrades to "sidecar keeps
    using its bundled/cached copy" if it's ever unreachable.
    """
    return _load_catalog_feed()


# ---------------------------------------------------------------------------
# GET /models
# ---------------------------------------------------------------------------


@router.get("/")
async def list_models() -> list[object]:
    """Return the curated model registry.

    Returns an empty list until the model registry is seeded
    (model_service.py + seed script).
    """
    # TODO: return await ModelService.list_published()
    return []


# ---------------------------------------------------------------------------
# GET /models/{slug}/manifest
# ---------------------------------------------------------------------------


@router.get("/{slug}/manifest")
async def get_model_manifest(slug: str) -> dict[str, object]:
    """Return a signed model manifest for download by the desktop app.

    Not implemented yet.
    """
    # TODO: call ModelService.get_manifest(slug)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Model {slug!r} not found",
    )
