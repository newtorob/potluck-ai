"""Platform-specific path helpers for the Potluck data directory.

All paths are rooted at ~/.potluck (overridable via POTLUCK_DATA_DIR).
"""

from __future__ import annotations

import os
from pathlib import Path


def _base() -> Path:
    """Return the root data directory, respecting POTLUCK_DATA_DIR override."""
    override = os.environ.get("POTLUCK_DATA_DIR")
    return Path(override) if override else Path.home() / ".potluck"


def data_dir() -> Path:
    """~/.potluck (or POTLUCK_DATA_DIR override)."""
    return _base()


def models_dir() -> Path:
    """~/.potluck/models - GGUF weight files live here."""
    return _base() / "models"


def logs_dir() -> Path:
    """~/.potluck/logs - rotated log files."""
    return _base() / "logs"


def config_path() -> Path:
    """~/.potluck/config.json - user settings."""
    return _base() / "config.json"


def db_path() -> Path:
    """~/.potluck/data/chat_history.db - optional SQLite chat store."""
    return _base() / "data" / "chat_history.db"


def memory_db_path() -> Path:
    """~/.potluck/data/memory.db - persistent memory and task-state store."""
    return _base() / "data" / "memory.db"


def catalog_cache_path() -> Path:
    """~/.potluck/data/model_catalog_cache.json - cached cloud model-catalog feed.

    Written only by services.catalog_feed's background refresh. Never the
    bundled offline floor on its own - model_registry merges this against
    the packaged potluck_local/data/models.json and prefers whichever is
    newer/valid (see model_registry._load_catalog).
    """
    return _base() / "data" / "model_catalog_cache.json"
