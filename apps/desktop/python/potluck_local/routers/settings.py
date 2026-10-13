"""Settings router.

GET   /settings - read current settings
PATCH /settings - merge-update settings
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from potluck_local.services.settings_manager import (
    AppSettings,
    GatewaySettings,
    MemorySettings,
    NetworkSettings,
    PrivacySettings,
    PotluckSettings,
    RuntimeSettings,
    VaultSettings,
    get_settings,
    save_settings,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Map of top-level key → sub-model type for typed nested merging.
_SUB_MODELS: dict[str, type] = {
    "runtime": RuntimeSettings,
    "privacy": PrivacySettings,
    "network": NetworkSettings,
    "app": AppSettings,
    "memory": MemorySettings,
    "vault": VaultSettings,
    "gateway": GatewaySettings,
}

# Top-level keys that are plain dicts (no Pydantic sub-model) and must be
# key-merged rather than replaced when a PATCH arrives. Without this,
# PATCH {ui: {k: v}} would wipe every other key already in the dict.
_DICT_MERGE_FIELDS: frozenset[str] = frozenset({"ui"})


@router.get("")
async def read_settings() -> dict[str, Any]:
    return get_settings().model_dump()


@router.patch("")
async def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    current = get_settings()
    current_data = current.model_dump()

    for key, value in patch.items():
        if key not in PotluckSettings.model_fields:
            continue  # silently drop unknown top-level keys

        if key in _SUB_MODELS and isinstance(value, dict):
            # Shallow-merge into the existing Pydantic sub-model so callers
            # can update a single field without supplying the full object.
            existing_sub = current_data.get(key, {})
            merged_sub = {**existing_sub, **value}
            try:
                current_data[key] = _SUB_MODELS[key](**merged_sub).model_dump()
            except Exception:
                logger.exception(
                    "settings_sub_model_validation_failed",
                    extra={"event": "settings_sub_model_validation_failed", "key": key},
                )
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid value for settings key '{key}', check sidecar logs",
                )
        elif key in _DICT_MERGE_FIELDS and isinstance(value, dict):
            # Plain-dict merge: graft incoming keys into the existing dict
            # without replacing the whole dict.  PATCH {ui: {k: v}} must
            # never silently drop other keys already in ui.
            existing_dict = current_data.get(key) or {}
            if isinstance(existing_dict, dict):
                current_data[key] = {**existing_dict, **value}
            else:
                current_data[key] = value
        else:
            current_data[key] = value

    try:
        updated = PotluckSettings(**current_data)
    except Exception:
        logger.exception(
            "settings_validation_failed",
            extra={"event": "settings_validation_failed"},
        )
        raise HTTPException(
            status_code=422,
            detail="Settings update failed, check sidecar logs",
        )

    save_settings(updated)
    logger.info("settings_updated")

    # The Contribute compute toggle (and the coordinator URL / job cap next to
    # it) drive the in-process contributor. Reconcile after every save; the
    # service is idempotent and a failure here must never fail the PATCH.
    try:
        from potluck_local.services.contributor_service import get_contributor_service

        await get_contributor_service().apply_settings(updated)
    except Exception:  # noqa: BLE001
        logger.exception("contributor_apply_settings_failed")

    return updated.model_dump()


@router.post("/gateway/rotate")
async def rotate_gateway_key() -> dict[str, Any]:
    """Generate a fresh gateway API key, enable the gateway, and persist it.

    Returns the new key so the UI can show it once for copying. Rotating
    invalidates the previous key. Gated by the same sidecar-token surface as the
    rest of /settings, so only the local app can call it."""
    import secrets

    current = get_settings()
    data = current.model_dump()
    new_key = "sk-potluck-" + secrets.token_hex(24)
    # Carry forward existing gateway settings (e.g. pool_routing); only the key
    # and enabled flag change on rotation. Rebuilding from scratch would silently
    # drop a user's opt-ins.
    data["gateway"] = current.gateway.model_copy(
        update={"enabled": True, "api_key": new_key}
    ).model_dump()
    updated = PotluckSettings(**data)
    save_settings(updated)
    logger.info("gateway_key_rotated")
    return {"enabled": True, "api_key": new_key}


@router.post("/gateway/enable")
async def enable_gateway() -> dict[str, Any]:
    """Idempotent enable: keep existing clients' key and routing preferences."""
    current = get_settings()
    if not current.gateway.api_key:
        await rotate_gateway_key()
    elif not current.gateway.enabled:
        save_settings(current.model_copy(update={
            "gateway": current.gateway.model_copy(update={"enabled": True})}))
    return {"enabled": True, "key_configured": True}
