"""Model registry — curated list of downloadable models for the desktop app.

Task: 8

Responsibilities:
- list_published() -> list[ModelRegistryEntry]  — returns active model rows
- get_manifest(slug) -> dict  — returns signed download manifest
  (URL, SHA256, size, signing key)

The manifest signing key is stored server-side; the desktop app verifies
downloads before loading weights.
"""
from __future__ import annotations


class ModelService:
    pass
