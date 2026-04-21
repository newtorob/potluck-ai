"""Settings router.

GET   /settings  — read current settings
PATCH /settings  — merge-update settings

Delegates to services/settings_manager (guide §4.4).
"""

from __future__ import annotations

from fastapi import APIRouter

from potluck_local.services.settings_manager import PotluckSettings, get_settings, save_settings

router = APIRouter()


@router.get("")
async def read_settings() -> dict[str, object]:
    """Return current settings as a plain dict.

    Reads from ~/.potluck/config.json; returns defaults if the file is absent.
    """
    return get_settings().model_dump()


@router.patch("")
async def update_settings(patch: dict[str, object]) -> dict[str, object]:
    """Merge-update settings and persist to disk.

    Accepts a partial settings dict and deep-merges it over the current state.

    TODO: Validate patch against PotluckSettings sub-models and apply runtime
          changes (e.g., reload model if context_size changes).
    """
    current = get_settings()
    # Merge patch into current settings using model_copy(update=...)
    merged_data = {**current.model_dump(), **patch}
    updated = PotluckSettings(**merged_data)
    save_settings(updated)
    return updated.model_dump()
