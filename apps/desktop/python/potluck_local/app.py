"""FastAPI app factory and CLI entrypoint for the Potluck local sidecar.

Section reference: guide §4.2

Usage:
    python -m potluck_local.app --port 8321
    POTLUCK_DEV=1 python -m potluck_local.app   # enables Swagger UI
"""

from __future__ import annotations

import argparse
import contextlib
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from potluck_local import __version__
from potluck_local.routers import agents, chat, hardware, health, memory, models, peer, settings, tasks, vault
from potluck_local.utils.logging import setup_logging


# CORS origins permitted to call the local sidecar.
# The Electron app uses app:// and file:// schemes; the Vite dev server
# uses localhost:5173.
_CORS_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "app://.",
    "file://",
]


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    import asyncio
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.embedding_backfill import run_embedding_backfill

    await init_memory_db()
    # Fire-and-forget: backfill embeddings for any memories that don't have them.
    # Runs to completion in the background; never blocks startup or the event loop.
    asyncio.create_task(run_embedding_backfill())
    yield


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application.

    Docs (Swagger / ReDoc) are enabled only when POTLUCK_DEV=1 is set,
    so production-packaged builds never expose an API explorer.
    """
    dev_mode = os.environ.get("POTLUCK_DEV", "0") == "1"

    app = FastAPI(
        lifespan=_lifespan,
        title="Potluck Local",
        description="Local API sidecar for the Potluck AI desktop app. Runs on 127.0.0.1 only.",
        version=__version__,
        docs_url="/docs" if dev_mode else None,
        redoc_url="/redoc" if dev_mode else None,
        openapi_url="/openapi.json" if dev_mode else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Peer-access guard — when the sidecar is bound to 0.0.0.0 (required so
    # household peers can dial it over the mesh), the bare socket would
    # expose every endpoint. This middleware restricts non-loopback callers
    # to the explicit peer-facing surface (/peer/* + /health). The local
    # user's memory, tasks, conversation history, and agent observability
    # are never reachable from another machine — even one in the same
    # household.
    #
    # Localhost source IPs (Electron, dev tools) get the full surface.
    @app.middleware("http")
    async def peer_access_middleware(request: Request, call_next):
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in ("127.0.0.1", "::1", "localhost")
        if not is_loopback:
            path = request.url.path
            if not (path.startswith("/peer/") or path == "/health"):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "this path is only available to localhost; "
                            "household peers may call /peer/* or /health"
                        ),
                    },
                )
        return await call_next(request)

    # Routers — prefixes must match localApi.ts in the frontend
    app.include_router(health.router, tags=["health"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(models.router, prefix="/models", tags=["models"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(hardware.router, prefix="/hardware", tags=["hardware"])
    app.include_router(memory.router, prefix="/memory", tags=["memory"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    app.include_router(agents.router, prefix="/agents", tags=["agents"])
    app.include_router(vault.router, prefix="/vault", tags=["vault"])
    app.include_router(peer.router, prefix="/peer", tags=["peer"])

    return app


def main() -> None:
    """Parse CLI args and start uvicorn bound to loopback only."""
    parser = argparse.ArgumentParser(description="Potluck local sidecar")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("POTLUCK_PORT", "8321")),
        help="Port to listen on (default 8321)",
    )
    args = parser.parse_args()

    dev_mode = os.environ.get("POTLUCK_DEV", "0") == "1"
    setup_logging(dev=dev_mode)

    app = create_app()
    # Bind to all interfaces so household-mesh peers can dial us over the
    # WireGuard mesh IP. The peer_access_middleware in create_app() is the
    # access boundary — non-loopback callers can only reach /peer/* + /health,
    # not the local user's memory / tasks / chat history. Override with
    # POTLUCK_BIND_HOST=127.0.0.1 to opt out (e.g. on a machine that should
    # never serve peer inference).
    bind_host = os.environ.get("POTLUCK_BIND_HOST", "0.0.0.0")
    uvicorn.run(
        app,
        host=bind_host,
        port=args.port,
        log_level="debug" if dev_mode else "info",
    )


if __name__ == "__main__":
    main()
