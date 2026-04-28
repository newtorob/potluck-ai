from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import sentry_sdk
import structlog
from fastapi import FastAPI

from coordinator.config import get_settings
from coordinator.db.database import close_db, init_db
from coordinator.routers import health, peers, transparency

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

    yield

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

    app.include_router(health.router)
    app.include_router(peers.router)
    app.include_router(transparency.router)

    return app
