from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from potluck_api import __version__
from potluck_api.config import get_settings
from potluck_api.db.database import check_database

router = APIRouter(tags=["health"])


@router.get("/health", response_class=JSONResponse)
async def health_check() -> dict[str, str]:
    """Liveness probe.  No auth, no rate limiting, no DB dependency.

    Returns version and current environment so operators can confirm the
    right build is running.
    """
    settings = get_settings()
    return {
        "status": "ok",
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
