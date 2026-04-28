from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from coordinator import __version__
from coordinator.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", response_class=JSONResponse)
async def health_check() -> dict[str, str]:
    """Liveness probe. No auth, no rate limiting, no DB dependency."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "coordinator",
        "version": __version__,
        "env": settings.env,
    }
