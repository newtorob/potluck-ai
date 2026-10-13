from __future__ import annotations

from slowapi import Limiter  # type: ignore[import-untyped]
from slowapi.util import get_remote_address  # type: ignore[import-untyped]
from starlette.requests import Request


def client_ip(request: Request) -> str:
    """Rate-limit key: the real client address, not the proxy in front of us.

    Behind Fly's proxy, ``request.client.host`` (what ``get_remote_address``
    returns) is Fly's edge IP, so every caller collapses to one key and the
    per-address limit becomes global. Fly sets ``Fly-Client-IP`` to the real
    client address and *overwrites* any client-supplied value, so it is
    trustworthy on Fly. We deliberately do not trust ``X-Forwarded-For`` as a
    fallback - it is client-spoofable, and letting a caller rotate the key would
    defeat the abuse guard on the magic-link endpoint. Off-Fly (local/dev) we
    fall back to the socket peer.

    NOTE: the limiter storage is in-memory (per process), so the API must run as
    a single machine at launch for the counts to be coherent. Scale-out needs a
    shared store (e.g. Redis) wired via ``storage_uri`` first.
    """
    fly_ip = request.headers.get("Fly-Client-IP")
    if fly_ip:
        return fly_ip.strip()
    return get_remote_address(request)


# Default limit applies to all routes unless overridden with @limiter.limit().
# Auth endpoints override this to 10/minute (applied in routers/auth.py).
# Webhook and health routes are exempt (no decorator).
limiter: Limiter = Limiter(
    key_func=client_ip,
    default_limits=["60/minute"],
)
