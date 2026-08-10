from __future__ import annotations

from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

CLIENT_VERSION_HEADER = "x-potluck-client-version"


class ClientVersionMiddleware(BaseHTTPMiddleware):
    """Reads the optional ``X-Potluck-Client-Version`` header off every request.

    Observability only — no enforcement. The desktop app self-blocks against
    GET /client/version when it's too old; the server never rejects a request
    for having a missing, malformed, or stale version here. When the header
    is present we bind it into structlog's contextvars for the life of the
    request, so it shows up on any structured log line emitted while handling
    it, without every route handler having to thread it through by hand.
    Absence of the header is the normal case for any client that predates
    this handshake and must never 400/401.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:  # type: ignore[override]
        client_version = request.headers.get(CLIENT_VERSION_HEADER)
        if not client_version:
            return await call_next(request)
        with structlog.contextvars.bound_contextvars(client_version=client_version):
            return await call_next(request)
