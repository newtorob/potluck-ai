"""Health check router.

GET /health — liveness probe used by Electron to confirm the sidecar is up.
"""

from fastapi import APIRouter

from potluck_local import __version__
from potluck_local.services.runtime import LLMRuntime

router = APIRouter()

# Module-level runtime instance shared with other routers.
# A singleton approach is fine here because the sidecar is a single-process
# server serving one user.  Real DI via app state is a later refactor.
_runtime = LLMRuntime()


@router.get("/health")
async def health() -> dict[str, object]:
    """Return sidecar liveness status.

    The frontend polls this on startup to know when the sidecar is ready,
    and checks model_loaded to decide whether to enable the chat UI.
    """
    return {
        "status": "ok",
        "version": __version__,
        "model_loaded": _runtime.is_loaded(),
    }
