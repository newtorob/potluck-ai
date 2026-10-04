from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from coordinator import __version__
from coordinator.config import get_settings
from coordinator.db.database import check_database

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


@router.get("/ready", response_class=JSONResponse)
async def readiness_check() -> JSONResponse:
    """Dependency readiness, separate from process liveness."""
    try:
        await check_database()
    except Exception:
        # Connection errors can contain credentials or internal addresses.
        return JSONResponse({"status": "unavailable", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})
