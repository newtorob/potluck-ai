"""Health check and graceful shutdown routes."""

from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter, BackgroundTasks

from potluck_local import __version__
from potluck_local.services.runtime import get_runtime

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe — frontend polls this to confirm the sidecar is up."""
    runtime = get_runtime()
    rpc_servers = runtime.loaded_rpc_servers()
    return {
        "status": "ok",
        "version": __version__,
        "model_loaded": runtime.is_loaded(),
        "active_model_id": runtime.current_model_id(),
        "n_ctx_loaded": runtime.loaded_n_ctx(),
        # Big Model Mode status — list of llama.cpp RPC endpoints the
        # current model is loaded across. Null when the model is local-only
        # or no model is loaded. Frontend uses this as the source of truth
        # for whether the toggle in HouseholdView shows on/off.
        "loaded_rpc_servers": list(rpc_servers) if rpc_servers else None,
    }


@router.post("/shutdown")
async def shutdown(background_tasks: BackgroundTasks) -> dict[str, str]:
    """Ask the sidecar to shut down cleanly.

    Electron posts here before sending SIGKILL so in-flight inference can finish.
    """

    def _terminate() -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    background_tasks.add_task(asyncio.get_event_loop().run_in_executor, None, _terminate)
    return {"status": "shutting_down"}
