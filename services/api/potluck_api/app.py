from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import sentry_sdk
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler  # type: ignore[import-untyped]
from slowapi.errors import RateLimitExceeded

from potluck_api.config import get_settings
from potluck_api.db.database import close_db, init_db
from potluck_api.middleware.client_version import ClientVersionMiddleware
from potluck_api.middleware.rate_limit import limiter
from potluck_api.routers import (
    admin,
    auth,
    device_auth,
    billing,
    client,
    discourse_sso,
    governance,
    health,
    members,
    models,
    webhooks,
)

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
        title="Potluck API",
        version="0.1.0",
        description="Potluck AI cloud backend. Handles auth, membership, billing, model registry, governance. Never sees prompts.",
        lifespan=lifespan,
        # Disable interactive docs outside development so we don't leak schema in prod.
        docs_url="/docs" if settings.env == "development" else None,
        redoc_url="/redoc" if settings.env == "development" else None,
        openapi_url="/openapi.json" if settings.env == "development" else None,
    )

    # ------------------------------------------------------------------ #
    # Rate limiting                                                        #
    # ------------------------------------------------------------------ #
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # CORS                                                                 #
    # ------------------------------------------------------------------ #
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://trypotluck.ai",
            "http://localhost:5173",
            "app://.",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Reads the optional X-Potluck-Client-Version header off every request for
    # observability (see middleware/client_version.py). Never rejects a request.
    app.add_middleware(ClientVersionMiddleware)

    # ------------------------------------------------------------------ #
    # Routers                                                              #
    # ------------------------------------------------------------------ #
    # Health - no prefix, no auth
    app.include_router(health.router)

    # Client version handshake - GET /client/version
    app.include_router(client.router, prefix="/client")

    # Auth endpoints live at /auth/...
    # discourse_sso also hangs off /auth (GET /auth/discourse-sso)
    app.include_router(auth.router, prefix="/auth")
    app.include_router(device_auth.router, prefix="/auth/device")
    app.include_router(discourse_sso.router, prefix="/auth")

    # Member profile - routes are /me and /me/...
    app.include_router(members.router)

    # Billing - /billing/...
    app.include_router(billing.router, prefix="/billing")

    # Webhooks - /webhooks/...
    app.include_router(webhooks.router, prefix="/webhooks")

    # Model registry - /models and /models/:slug/manifest
    app.include_router(models.router, prefix="/models")

    # Governance - routes are /proposals, /votes/:id
    app.include_router(governance.router)

    # Admin - /admin/...  (all routes gated by require_admin dependency)
    app.include_router(admin.router, prefix="/admin")

    return app
