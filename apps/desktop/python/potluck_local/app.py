"""FastAPI app factory and CLI entrypoint for the Potluck local sidecar.

Section reference: guide §4.2

Usage:
    python -m potluck_local.app --port 8321
    POTLUCK_DEV=1 python -m potluck_local.app   # enables Swagger UI
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import ipaddress
import logging
import os
import posixpath

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from potluck_local import __version__
from potluck_local.routers import agents, chat, gateway, hardware, health, memory, models, ollama, peer, readiness, settings, tasks, vault, verify
from potluck_local.utils.logging import setup_logging

logger = logging.getLogger(__name__)


# CORS origins permitted to call the local sidecar.
# The Electron app uses app:// and file:// schemes; the Vite dev server
# uses localhost:5173.
_CORS_ORIGINS: list[str] = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "app://.",
    "file://",
]


# The WireGuard mesh subnet -- the trust boundary for /peer/* is "the caller is
# on the household's WireGuard mesh", NOT merely "the caller isn't loopback".
# main() below binds uvicorn to 0.0.0.0 so household peers can dial the mesh
# IP, which means the bare socket is ALSO reachable from anything else on the
# same LAN/Wi-Fi (coffee shop, dorm, apartment) -- "not loopback" and "on the
# mesh" are not the same claim.
#
# Source of truth for the actual allocation: the coordinator hands out mesh
# IPs from 100.64.0.0/10 (the CGNAT range -- chosen because it never collides
# with home/corp RFC1918 ranges), carving one /24 per household. See
# services/coordinator/coordinator/services/household_allocator.py
# (HOUSEHOLD_POOL) and coordinator/models/household.py. A stale comment in
# the 0001 migration says "10.42.0.0/16" -- that predates the CGNAT-range
# decision and does not match the allocator code; 100.64.0.0/10 is correct.
#
# Override via POTLUCK_MESH_CIDR for non-default deployments (must remain a
# superset of every household's allocated /24).
_DEFAULT_MESH_CIDR = "100.64.0.0/10"


def _resolve_mesh_network() -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """Parse POTLUCK_MESH_CIDR (or the default) once at app construction."""
    raw = os.environ.get("POTLUCK_MESH_CIDR", "").strip() or _DEFAULT_MESH_CIDR
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        logger.warning(
            "invalid_mesh_cidr value=%r falling back to default=%s", raw, _DEFAULT_MESH_CIDR
        )
        return ipaddress.ip_network(_DEFAULT_MESH_CIDR)


def _client_ip_in_network(
    client_host: str, network: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    """True when `client_host` parses as an IP address inside `network`.

    Unparseable input (empty string, hostnames, test-harness placeholders like
    "testclient") is treated as NOT in the mesh -- fail closed. IPv4-mapped
    IPv6 addresses (::ffff:a.b.c.d), which a dual-stack listen socket can
    report for an IPv4 peer, are unwrapped to their v4 form before the
    membership check so they still match a v4 mesh CIDR.
    """
    try:
        addr = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    try:
        return addr in network
    except TypeError:
        # Address family mismatch (e.g. v6 address vs v4 network) -- not a member.
        return False


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    import asyncio
    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.embedding_backfill import run_embedding_backfill
    from potluck_local.services.agent_observability import AgentObserver

    await init_memory_db()

    # Eagerly warm the hardware-detect cache so the first /hardware or
    # /peer/capabilities request never pays the blocking subprocess cost.
    # Runs in a thread-pool executor via the same helper used by both routers.
    from potluck_local.routers.peer import _cached_hardware
    asyncio.create_task(_cached_hardware())

    # Fire-and-forget: backfill embeddings for any memories that don't have them.
    # Runs to completion in the background; never blocks startup or the event loop.
    asyncio.create_task(run_embedding_backfill())

    # Stale-session sweep. MCP transports don't always send a clean disconnect
    # (crash, kill, network loss), so without this, rows pile up as "active"
    # forever and clutter the AgentsView. We do one immediate sweep at startup
    # to clean up anything stranded across a sidecar restart, then a periodic
    # 60s loop catches sessions that go stale during this run.
    observer = AgentObserver()
    await observer.sweep_stale_sessions(max_age_seconds=300)

    async def _stale_session_sweeper() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await observer.sweep_stale_sessions(max_age_seconds=300)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                # Don't let one failed sweep kill the loop.
                pass
    asyncio.create_task(_stale_session_sweeper())

    yield


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application.

    Docs (Swagger / ReDoc) are enabled only when POTLUCK_DEV=1 is set,
    so production-packaged builds never expose an API explorer.
    """
    dev_mode = os.environ.get("POTLUCK_DEV", "0") == "1"

    # Shared-secret gate for the local API. When the desktop app spawns this
    # sidecar it passes a random POTLUCK_SIDECAR_TOKEN, and the renderer attaches
    # it as `Authorization: Bearer <token>` on every request. This stops other
    # local processes on the machine from reading the user's memory, chat,
    # tasks, and vault over the loopback socket. When unset (e.g. `make sidecar`
    # standalone in dev), the gate is off and loopback callers get the full
    # surface as before.
    sidecar_token = os.environ.get("POTLUCK_SIDECAR_TOKEN") or None

    # Separate opt-in key for the OpenAI-compatible gateway (/v1/*), so external
    # tools (Cursor, etc.) authenticate without the internal sidecar token. Unset
    # means: gateway off in production (sidecar token set), open on loopback in
    # dev. Enforced in peer_access_middleware below.
    gateway_key = os.environ.get("POTLUCK_GATEWAY_API_KEY") or None

    # Mesh CIDR — the trust boundary /peer/* enforces (see _resolve_mesh_network
    # docstring for the source-of-truth rationale). Parsed once per app instance.
    mesh_network = _resolve_mesh_network()

    app = FastAPI(
        lifespan=_lifespan,
        title="Potluck Local",
        description="Local API sidecar for the Potluck AI desktop app. Runs on 127.0.0.1 only.",
        version=__version__,
        docs_url="/docs" if dev_mode else None,
        redoc_url="/redoc" if dev_mode else None,
        openapi_url="/openapi.json" if dev_mode else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Compress responses >= 1 KB when the client sends Accept-Encoding: gzip.
    # Keeps multi-row memory/export and trace payloads well under the wire.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Peer-access guard — when the sidecar is bound to 0.0.0.0 (required so
    # household peers can dial it over the mesh), the bare socket would
    # expose every endpoint. This middleware restricts non-loopback callers
    # to the explicit peer-facing surface (/peer/* + /health), and further
    # requires /peer/* callers' source IP to be inside the WireGuard mesh
    # CIDR (mesh_network above) — "non-loopback" is not the mesh boundary,
    # since 0.0.0.0 is reachable from any device on the same LAN/Wi-Fi, not
    # just mesh peers. The local user's memory, tasks, conversation history,
    # and agent observability are never reachable from another machine —
    # even one in the same household, let alone the same coffee shop.
    #
    # Localhost source IPs (Electron, dev tools) get the full surface.
    @app.middleware("http")
    async def peer_access_middleware(request: Request, call_next):
        raw_path = request.url.path
        # Reject null bytes — they can confuse downstream parsers.
        if "\x00" in raw_path:
            return JSONResponse(status_code=400, content={"detail": "invalid path"})
        # Normalise away any ../ traversal sequences once, up front, so both the
        # peer-surface restriction and the token gate match the real target path.
        normalised = posixpath.normpath(raw_path)
        # posixpath.normpath preserves a leading "//"; collapse it so the
        # gateway prefix check and the exempt/peer checks see one canonical path.
        normalised = "/" + normalised.lstrip("/")

        # Trust the socket peer IP, and ONLY the socket peer IP. We deliberately
        # do not honor X-Forwarded-For / X-Real-IP here: the sidecar is dialed
        # directly (Electron over loopback, peers over the raw WireGuard mesh IP),
        # never through a reverse proxy. If a proxy is ever placed in front of the
        # sidecar, every request would present the proxy's loopback IP and get the
        # full local surface — so that must not happen without revisiting this gate.
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in ("127.0.0.1", "::1", "localhost")

        # Non-loopback callers (household peers over the mesh) are restricted to
        # the explicit peer-facing surface (/peer/* + /health). The local user's
        # memory, tasks, conversation history, and agent observability are never
        # reachable from another machine, even one in the same household.
        if not is_loopback:
            if not (normalised.startswith("/peer/") or normalised == "/health"):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "this path is only available to localhost; "
                            "household peers may call /peer/* or /health"
                        ),
                    },
                )

            # /peer/* additionally requires the caller's source IP to be on the
            # WireGuard mesh (see mesh_network above) -- "non-loopback" alone is
            # NOT the mesh boundary: uvicorn binds 0.0.0.0 (see main()), so a
            # device on the same Wi-Fi/LAN is also "non-loopback" without ever
            # having joined the household's mesh. /peer/capabilities,
            # /peer/chat/completions, and (when opted in) /peer/memory/search --
            # which returns full memory content -- must never be reachable by a
            # bystander on the coffee-shop/dorm/apartment network. /health stays
            # on the pre-existing loopback-or-any-caller liveness exemption (no
            # memory or inference surface there).
            if normalised.startswith("/peer/") and not _client_ip_in_network(
                client_host, mesh_network
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "peer routes require the caller to be on the "
                            "household WireGuard mesh"
                        ),
                    },
                )

        # OpenAI/Ollama-compatible gateway (/v1/*) is for external tools that do
        # not carry the internal sidecar token. It has its own opt-in key and
        # bypasses the sidecar-token gate below. Already loopback-only via the
        # non-loopback block above (remote callers get /peer/* + /health only).
        if (
            normalised.startswith("/v1/")
            or normalised == "/v1"
            or normalised.startswith("/api/")
            or normalised == "/api"
        ):
            # Effective key: env override first, else the live setting (so the
            # Settings toggle / key rotation take effect with no restart).
            effective_key = gateway_key
            if effective_key is None:
                try:
                    from potluck_local.services.settings_manager import get_settings as _gs

                    gw = _gs().gateway
                    if gw.enabled and gw.api_key:
                        effective_key = gw.api_key
                except Exception:
                    effective_key = None
            if effective_key is not None:
                auth = request.headers.get("authorization", "")
                presented = auth[7:] if auth[:7].lower() == "bearer " else ""
                if not (presented and hmac.compare_digest(presented, effective_key)):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "missing or invalid gateway API key"},
                    )
                return await call_next(request)
            # Gateway not configured: open only when the sidecar gate is off (dev).
            # In production (token set) it stays disabled until the user enables it.
            if sidecar_token is not None:
                return JSONResponse(
                    status_code=404,
                    content={"detail": "gateway disabled; enable it in Settings -> Connect tools"},
                )
            return await call_next(request)

        # Shared-secret gate — applies to loopback callers too. When a token is
        # configured, every non-exempt path must present a matching bearer token.
        # This is what stops a malicious local process from reading the user's
        # data over 127.0.0.1. Exempt: liveness (/health), peer routes, and
        # /shutdown (Electron may call it from the main process during teardown).
        #
        # NOTE on /peer/* exemption: peer routes are NOT per-request signature
        # authenticated in v0. Their credential is mesh membership — a caller can
        # only reach them from a source IP inside the WireGuard mesh CIDR (Gate 1
        # above already 403s any non-loopback path outside /peer/* + /health, and
        # the mesh-CIDR check just above that 403s a /peer/* caller whose source
        # IP is outside the mesh subnet), and mesh enrollment is gated by the
        # coordinator. Per-request signed-nonce attestation is a v0.5+ item. See
        # routers/peer.py "Trust model". The exemption is safe because the peer
        # surface never exposes local memory/tasks/chat and the one memory-read
        # route is opt-in and off by default.
        if sidecar_token is not None:
            exempt = (
                normalised == "/health"
                or normalised == "/shutdown"
                or normalised.startswith("/peer/")
            )
            if not exempt:
                auth = request.headers.get("authorization", "")
                presented = auth[7:] if auth[:7].lower() == "bearer " else ""
                if not (presented and hmac.compare_digest(presented, sidecar_token)):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "missing or invalid sidecar token"},
                    )

        return await call_next(request)

    # Routers — prefixes must match localApi.ts in the frontend
    app.include_router(health.router, tags=["health"])
    app.include_router(readiness.router, tags=["readiness"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(models.router, prefix="/models", tags=["models"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(hardware.router, prefix="/hardware", tags=["hardware"])
    app.include_router(memory.router, prefix="/memory", tags=["memory"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    app.include_router(agents.router, prefix="/agents", tags=["agents"])
    app.include_router(vault.router, prefix="/vault", tags=["vault"])
    app.include_router(peer.router, prefix="/peer", tags=["peer"])
    app.include_router(verify.router, prefix="/verify", tags=["verify"])
    app.include_router(gateway.router, prefix="/v1", tags=["gateway"])
    app.include_router(ollama.router, prefix="/api", tags=["ollama"])

    return app


def main() -> None:
    """Parse CLI args and start uvicorn bound to loopback only."""
    parser = argparse.ArgumentParser(description="Potluck local sidecar")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("POTLUCK_PORT", "8321")),
        help="Port to listen on (default 8321)",
    )
    args = parser.parse_args()

    dev_mode = os.environ.get("POTLUCK_DEV", "0") == "1"
    setup_logging(dev=dev_mode)

    app = create_app()
    # Bind to all interfaces so household-mesh peers can dial us over the
    # WireGuard mesh IP. The peer_access_middleware in create_app() is the
    # access boundary — non-loopback callers can only reach /peer/* + /health,
    # not the local user's memory / tasks / chat history. Override with
    # POTLUCK_BIND_HOST=127.0.0.1 to opt out (e.g. on a machine that should
    # never serve peer inference).
    bind_host = os.environ.get("POTLUCK_BIND_HOST", "0.0.0.0")
    uvicorn.run(
        app,
        host=bind_host,
        port=args.port,
        log_level="debug" if dev_mode else "info",
    )


if __name__ == "__main__":
    main()
