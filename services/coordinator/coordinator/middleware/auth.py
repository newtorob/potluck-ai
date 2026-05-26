"""Coordinator bearer-token authentication dependency.

v0 stub: a single shared secret via the ``POTLUCK_COORDINATOR_TOKEN``
environment variable, compared in constant time. When the variable is
absent the dependency is a no-op (dev mode passthrough). A one-shot
WARNING is emitted at startup (in ``app.py``'s lifespan) so operators
notice the unauthenticated posture before any network exposure.

v0.5 will replace this with signed-nonce session auth per
memory cba2143d.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def require_coordinator_token(
    x_potluck_coordinator_token: str | None = Header(
        default=None,
        alias="X-Potluck-Coordinator-Token",
    ),
) -> None:
    """FastAPI dependency — enforces bearer-style token auth.

    Rules:
    * If ``POTLUCK_COORDINATOR_TOKEN`` is unset or empty → dev-mode
      passthrough. Every request is allowed. A WARNING is logged once
      at startup (see ``app.py``) so the operator is aware.
    * If the env var is set → the ``X-Potluck-Coordinator-Token`` header
      must be present and must match via constant-time comparison.
      Missing or wrong → 401.

    Constant-time comparison (``hmac.compare_digest``) prevents timing
    attacks that could leak information about the expected token length
    or prefix.
    """
    expected = os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip()
    if not expected:
        # Dev mode: no token configured. Allow all requests through.
        # The startup warning in app.py's lifespan handles operator visibility.
        return

    provided = (x_potluck_coordinator_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
