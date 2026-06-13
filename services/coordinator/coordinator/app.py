from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from coordinator.config import get_settings
from coordinator.db.database import close_db, init_db
from coordinator.routers import health, jobs, me, mesh, peers, transparency

# Origins permitted to call the coordinator's HTTP endpoints from a
# browser context (the Electron renderer counts). Curl + the WS handshake
# are unaffected by CORS so this only matters for the desktop chat UI
# hitting POST /jobs/stream from the renderer process.
_CORS_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "app://.",
    "file://",
]

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=0.1,
            environment=settings.env,
        )
        logger.info("sentry_initialized", dsn_prefix=settings.sentry_dsn[:20])

    await init_db()
    logger.info("database_pool_initialized")

    if not os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip():
        logger.warning(
            "POTLUCK_COORDINATOR_TOKEN not set — coordinator running in "
            "unauthenticated dev mode. Set this env var before any network exposure."
        )

    # Background credit-verification loop: periodically probe online peers that
    # hold escrowed credits (canary -> reputation -> escrow release).
    verification_stop = asyncio.Event()
    verification_task: asyncio.Task | None = None
    if settings.verification_probe_enabled:
        from coordinator.services.verification import run_verification_loop

        verification_task = asyncio.create_task(
            run_verification_loop(verification_stop)
        )
        logger.info("verification_loop_scheduled")

    yield

    verification_stop.set()
    if verification_task is not None:
        verification_task.cancel()
        try:
            await verification_task
        except asyncio.CancelledError:
            pass

    await close_db()
    logger.info("database_pool_closed")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Potluck Coordinator",
        version="0.1.0",
        description=(
            "Phase G coordinator: peer registry, attestation, job routing, "
            "credit ledger, mesh control plane. Never sees prompt content."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.env == "development" else None,
        redoc_url="/redoc" if settings.env == "development" else None,
        openapi_url="/openapi.json" if settings.env == "development" else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(peers.router)
    app.include_router(transparency.router)
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(mesh.router)
    app.include_router(me.router)

    return app
