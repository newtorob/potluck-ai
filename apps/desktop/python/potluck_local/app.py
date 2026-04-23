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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from potluck_local import __version__
from potluck_local.routers import agents, chat, hardware, health, memory, models, settings, tasks
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
    from potluck_local.services.memory_db import init_memory_db
    await init_memory_db()
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

    # Routers — prefixes must match localApi.ts in the frontend
    app.include_router(health.router, tags=["health"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(models.router, prefix="/models", tags=["models"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(hardware.router, prefix="/hardware", tags=["hardware"])
    app.include_router(memory.router, prefix="/memory", tags=["memory"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    app.include_router(agents.router, prefix="/agents", tags=["agents"])

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
    uvicorn.run(
        app,
        host="127.0.0.1",  # loopback only — never exposed to the network
        port=args.port,
        log_level="debug" if dev_mode else "info",
    )


if __name__ == "__main__":
    main()
