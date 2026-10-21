"""Cloud model-catalog feed - background refresh + validated disk cache.

The bundled catalog (potluck_local/data/models.json) is the offline floor:
it ships inside the packaged app and always works with zero network. This
module is the sidecar's only network-touching piece of the catalog story.
It periodically GETs {POTLUCK_CLOUD_API_BASE}/models/catalog, validates the
response, and writes it to a disk cache (utils.paths.catalog_cache_path()) -
it never overwrites the bundled data file. model_registry._load_catalog()
then picks the freshest valid source between that cache and the bundled
file; request handlers never block on the network here.

Never raises: every failure (offline, DNS, non-2xx, malformed JSON, missing
required fields, the cloud endpoint not deployed yet) is caught, logged at
info/warning, and simply means the refresh made no progress this cycle - the
cache keeps whatever it already had (or stays absent, and callers fall back
to bundled).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from potluck_local.config import get_local_settings
from potluck_local.utils.paths import catalog_cache_path

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 6 * 60 * 60  # 6h
_REQUEST_TIMEOUT_SECONDS = 10.0


_HF_RESOLVE_REVISION = re.compile(r"^https://huggingface\.co/[^/]+/[^/]+/resolve/([^/]+)/")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def is_pinned_download_url(url: str) -> bool:
    """False for a Hugging Face `resolve/<ref>/` URL whose ref is not a full
    40-hex commit (`main`, a branch, a tag): those refs move, so the bytes
    behind the URL can change after review. Non-HF URLs are not judged here."""
    match = _HF_RESOLVE_REVISION.match(url)
    return match is None or _COMMIT_SHA.match(match.group(1)) is not None


def _validate(data: Any) -> bool:
    """True when `data` is a structurally valid catalog feed payload:
    a dict carrying schema_version and a non-empty models list, every entry
    of which has at minimum slug + download_url + sha256, and whose
    Hugging Face download_url is pinned to a commit (never `resolve/main`)."""
    if not isinstance(data, dict):
        return False
    if "schema_version" not in data:
        return False
    models = data.get("models")
    if not isinstance(models, list) or not models:
        return False
    for entry in models:
        if not isinstance(entry, dict):
            return False
        if not (entry.get("slug") and entry.get("download_url") and entry.get("sha256")):
            return False
        if not is_pinned_download_url(str(entry["download_url"])):
            return False
    return True


async def _fetch(url: str) -> Any:
    """Isolated so tests can monkeypatch just the network call."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def refresh_once() -> bool:
    """Fetch the cloud catalog feed once and write it to disk if valid.

    Returns True when a new valid catalog was written, False on any failure
    (offline, non-2xx, malformed body, cache write error) - never raises
    (except asyncio.CancelledError, which must propagate for shutdown).
    """
    base = get_local_settings().cloud_api_base.rstrip("/")
    url = f"{base}/models/catalog"
    try:
        data = await _fetch(url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/parse errors are expected (offline, not yet deployed)
        logger.info("catalog_feed_refresh_failed url=%s error=%s", url, exc)
        return False

    if not _validate(data):
        logger.warning("catalog_feed_refresh_invalid url=%s", url)
        return False

    cache_path = catalog_cache_path()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        logger.warning("catalog_feed_cache_write_failed path=%s", cache_path)
        return False

    logger.info("catalog_feed_refreshed count=%d", len(data.get("models", [])))
    # Drop model_registry's in-memory merge so the next list_models()/
    # get_model() call re-evaluates bundled-vs-cached-remote and picks up
    # this refresh immediately, without a sidecar restart.
    from potluck_local.services import model_registry

    model_registry.invalidate_cache()
    return True


async def refresh_loop() -> None:
    """Background loop: refresh once immediately, then every
    REFRESH_INTERVAL_SECONDS. Intended to be wrapped in app.py's lifespan
    background-task tracking so shutdown cancels it cleanly."""
    while True:
        try:
            await refresh_once()
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return


def read_cached_catalog() -> dict[str, Any] | None:
    """Return the cached remote catalog if present and structurally valid,
    else None. Never raises."""
    cache_path = catalog_cache_path()
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _validate(data):
        return None
    return data
