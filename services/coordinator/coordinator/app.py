from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from coordinator.config import get_settings
from coordinator.db.database import close_db, init_db
from coordinator.routers import health, jobs, me, mesh, peers, transparency, circles

# CORS controls which browser-context origins may call the coordinator's HTTP
# endpoints (the Electron renderer hitting POST /jobs/stream). Curl + the WS
# handshake are unaffected by CORS.
#
# The packaged Electron renderer loads from app://. / file:// in EVERY env, so
# those are always allowed. The Vite dev server (localhost:5173) is a
# development-only convenience and must not be shipped to staging/production.
_BASE_CORS_ORIGINS: list[str] = ["app://.", "file://"]
_DEV_CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


def resolve_cors_origins(settings) -> list[str]:
    """Effective CORS allowlist for the current environment: the always-on
    packaged-app origins, plus the Vite dev origins in development, plus any
    operator-configured CORS_ORIGINS. Order-preserving, de-duplicated."""
    origins = list(_BASE_CORS_ORIGINS)
    if settings.env == "development":
        origins += _DEV_CORS_ORIGINS
    origins += [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


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

    # Release-readiness preflight: logs the safety checklist every boot and, in
    # staging/production, refuses to start on an operator-fixable insecure
    # default (open token, TOFU keys, default DB creds). Advisory in development.
    # Runs BEFORE init_db so a refusal doesn't open (and leak) the DB pool.
    from coordinator.services.preflight import enforce_preflight

    enforce_preflight(settings)

    await init_db()
    logger.info("database_pool_initialized")

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
        allow_origins=resolve_cors_origins(settings),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(peers.router)
    app.include_router(transparency.router)
    app.include_router(jobs.router, prefix="/jobs")
    app.include_router(mesh.router)
    app.include_router(me.router)
    app.include_router(circles.router)

    return app
