from __future__ import annotations

from slowapi import Limiter  # type: ignore[import-untyped]
from slowapi.util import get_remote_address  # type: ignore[import-untyped]

# Default limit applies to all routes unless overridden with @limiter.limit().
# Auth endpoints override this to 10/minute (applied in routers/auth.py).
# Webhook and health routes are exempt (no decorator).
limiter: Limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
)
