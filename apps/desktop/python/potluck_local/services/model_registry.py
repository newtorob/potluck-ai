"""Curated model registry — fetches available models from the cloud API.

TODO: Implement per guide §4.2 / §4.3:
  - fetch_registry() -> list[dict]  — GET https://api.trypotluck.ai/models
    with offline fallback to potluck_local/data/models.json
  - get_manifest(model_id) -> dict  — GET .../models/{id}/manifest
  - Cache registry response in memory with a short TTL (e.g., 5 minutes)
    so repeated GET /models calls don't hammer the cloud API.
"""

from __future__ import annotations


class ModelRegistry:
    """Fetches and caches the curated model list from the cloud API."""

    pass
