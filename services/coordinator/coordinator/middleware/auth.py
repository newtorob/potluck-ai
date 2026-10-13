"""Coordinator bearer-token authentication dependency.

v0 stub: a single shared secret via the ``POTLUCK_COORDINATOR_TOKEN``
environment variable, compared in constant time. When the variable is
absent the dependency is a no-op (dev mode passthrough). A one-shot
WARNING is emitted at startup (in ``app.py``'s lifespan) so operators
notice the unauthenticated posture before any network exposure.

Signed-nonce session auth is planned to replace this.
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def coordinator_token_matches(provided: str | None) -> bool:
    """Return True if the caller is authorised to talk to the coordinator.

    * If ``POTLUCK_COORDINATOR_TOKEN`` is unset/empty → dev-mode passthrough
      (always True). Existing tokenless household coordinators are unaffected.
    * If set → ``provided`` must be present and match via constant-time compare.

    Shared by the HTTP dependency below and the WebSocket handlers
    (``/peers/connect``, future ``/mesh/signal``), which cannot use an
    ``HTTPException``-raising dependency and must check the token inline.
    """
    expected = os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip()
    if not expected:
        return True
    provided = (provided or "").strip()
    return bool(provided) and hmac.compare_digest(provided, expected)


def require_coordinator_token(
    x_potluck_coordinator_token: str | None = Header(
        default=None,
        alias="X-Potluck-Coordinator-Token",
    ),
) -> None:
    """FastAPI dependency - enforces bearer-style token auth on HTTP routes.

    Dev-mode passthrough when ``POTLUCK_COORDINATOR_TOKEN`` is unset; otherwise
    the ``X-Potluck-Coordinator-Token`` header must match (constant-time).
    """
    if not coordinator_token_matches(x_potluck_coordinator_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
