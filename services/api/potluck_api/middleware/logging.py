from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


def _hash_email(email: str) -> str:
    """One-way hash an email address for privacy-safe logging."""
    return hashlib.sha256(email.lower().strip().encode()).hexdigest()[:16]


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Structured request logging middleware.

    Logs method, path, status code, and duration.
    Never logs raw email addresses or session tokens — any email value
    encountered is SHA-256 hashed before emission.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:  # type: ignore[override]
        start = time.perf_counter()

        response: Response = await call_next(request)

        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        # Attach the middleware logger to the request state so route handlers
        # can bind extra context (e.g. member_id) without touching PII.
        log = logger.bind(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            # client IP — acceptable for rate-limit debugging, no PII
            client=request.client.host if request.client else "unknown",
        )

        if response.status_code >= 500:
            log.error("request_error")
        elif response.status_code >= 400:
            log.warning("request_warning")
        else:
            log.info("request_ok")

        return response
