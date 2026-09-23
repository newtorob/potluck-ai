"""Health check and graceful shutdown routes."""

from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter, HTTPException, Request

from potluck_local import __version__
from potluck_local.services.runtime import get_runtime
from potluck_local.services.model_lifecycle import get_model_lifecycle

router = APIRouter()


@router.get("/control")
async def control_info(request: Request) -> dict[str, object]:
    """Authenticated identity check. Never returns the control credential."""
    control = getattr(request.app.state, "runtime_control", None)
    if control is None:
        raise HTTPException(409, "Runtime was not started with shared control")
    return control.info()


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
        "model_restore": get_model_lifecycle().status(),
        "n_ctx_loaded": runtime.loaded_n_ctx(),
        # Big Model Mode status — list of llama.cpp RPC endpoints the
        # current model is loaded across. Null when the model is local-only
        # or no model is loaded. Frontend uses this as the source of truth
        # for whether the toggle in HouseholdView shows on/off.
        "loaded_rpc_servers": list(rpc_servers) if rpc_servers else None,
        # Pipeline-parallel status — describes the distributed engine (head layers,
        # downstream peer, device) when a model is loaded with the pipeline strategy;
        # null otherwise. The other WAN-viable "big model" path, sibling to RPC mode.
        "loaded_pipeline": runtime.loaded_pipeline(),
        # Shared (continuous) batching — true when the loaded model is served through a
        # continuous batcher so concurrent requests share batched forward passes. Only
        # the transformers paths (whole-model / pipeline) can be loaded this way; always
        # false on the single-stream + llama.cpp paths. The frontend reads this as the
        # source of truth for the shared-batching toggle.
        "continuous_batch": runtime.continuous_batch_active(),
    }


@router.post("/shutdown")
async def shutdown(request: Request) -> dict[str, str]:
    """Ask the sidecar to shut down cleanly.

    Electron posts here before sending SIGKILL so in-flight inference can finish.
    Unload the model first so ggml-metal does not assert during process exit.
    """
    import gc

    try:
        await get_model_lifecycle().stop()
        await get_runtime().unload_model_async()
        gc.collect()
    except Exception:  # noqa: BLE001
        # Still proceed to terminate — better a noisy exit than a hung sidecar.
        pass

    def _terminate() -> None:
        server = getattr(request.app.state, "runtime_server", None)
        if server is not None:
            # Let main() leave its control context and remove the private record.
            # Uvicorn re-raises captured SIGTERM after serving, which can otherwise
            # terminate the process before that context gets a chance to exit.
            server.should_exit = True
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    loop = asyncio.get_running_loop()
    loop.call_later(0.1, _terminate)
    return {"status": "shutting_down"}
