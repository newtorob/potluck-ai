from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter(tags=["models"])


# ---------------------------------------------------------------------------
# GET /models
# ---------------------------------------------------------------------------


@router.get("/")
async def list_models() -> list[object]:
    """Return the curated model registry.

    Returns an empty list until the model registry is seeded in Task 8
    (model_service.py + seed script).
    """
    # TODO (Task 8): return await ModelService.list_published()
    return []


# ---------------------------------------------------------------------------
# GET /models/{slug}/manifest
# ---------------------------------------------------------------------------


@router.get("/{slug}/manifest")
async def get_model_manifest(slug: str) -> dict[str, object]:
    """Return a signed model manifest for download by the desktop app.

    Full implementation in Task 8.
    """
    # TODO (Task 8): call ModelService.get_manifest(slug)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Model {slug!r} not found",
    )
