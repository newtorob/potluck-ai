"""FastAPI app factory and CLI entrypoint for the Potluck local sidecar.

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
from potluck_local.routers import account, agents, anthropic, batches, chat, chat_circle, circles, contributor, gateway, hardware, health, memory, models, ollama, peer, readiness, settings, tasks, usage, vault, verify
from potluck_local.utils.logging import setup_logging
from potluck_local.routers import split
from potluck_local.services import anthropic_messages, peer_trust
from potluck_local.services.split_native import SplitError

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
# on the household's WireGuard mesh", not merely "the caller isn't loopback".
# main() below binds uvicorn to 0.0.0.0 so household peers can dial the mesh
# IP, which means the bare socket is also reachable from anything else on the
# same LAN/Wi-Fi (coffee shop, dorm, apartment) -- "not loopback" and "on the
# mesh" are not the same claim.
#
# Source of truth for the actual allocation: the coordinator hands out mesh
# IPs from 100.64.0.0/10 (the CGNAT range -- chosen because it never collides
# with home/corp RFC1918 ranges), carving one /24 per household. See
# services/coordinator/coordinator/services/household_allocator.py
# (HOUSEHOLD_POOL) and coordinator/models/household.py.
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


# The Anthropic Messages routes answer in Anthropic's error shape, including the
# refusals made here before a request reaches them.
_MESSAGES_PATHS = ("/v1/messages", "/v1/messages/count_tokens")


def _refusal(path: str, status: int, detail: str) -> JSONResponse:
    """A refusal by the middleware. The Messages routes get {"type": "error", ...};
    every other path keeps {"detail": ...}."""
    if path in _MESSAGES_PATHS:
        return JSONResponse(status_code=status, content=anthropic_messages.error_body(status, detail))
    return JSONResponse(status_code=status, content={"detail": detail})


def _key_matches(presented: str, key: str) -> bool:
    """Constant-time comparison. Bytes, so a non-ASCII header is a mismatch and not an error."""
    return bool(presented) and hmac.compare_digest(presented.encode("utf-8", "replace"), key.encode("utf-8", "replace"))


def _client_ip_in_network(
    client_host: str, network: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    """True when `client_host` parses as an IP address inside `network`.

    Unparseable input (empty string, hostnames, test-harness placeholders like
    "testclient") is treated as not in the mesh -- fail closed. IPv4-mapped
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
    import gc
    import logging

    from potluck_local.services.memory_db import init_memory_db
    from potluck_local.services.embedding_backfill import run_embedding_backfill
    from potluck_local.services.agent_observability import AgentObserver
    from potluck_local.services.catalog_feed import refresh_loop as catalog_refresh_loop

    logger = logging.getLogger(__name__)

    await init_memory_db()

    # Background tasks spawned below must be retained, not fire-and-forgotten:
    # an unreferenced asyncio.create_task() is only kept alive by the event
    # loop's internal weak bookkeeping, and a test run (or a fast shutdown)
    # can close the loop while one is still scheduled - e.g. the stale-session
    # sweeper waking up from asyncio.sleep(60) after the TestClient's loop is
    # gone, which shows up as an aiosqlite worker thread hitting a closed
    # event loop. Every task we create here goes into background_tasks so the
    # shutdown path below can cancel + await (drain) all of them before the
    # model/DB teardown runs.
    background_tasks: set[asyncio.Task] = set()

    def _track(task: asyncio.Task) -> asyncio.Task:
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    # Eagerly warm the hardware-detect cache so the first /hardware or
    # /peer/capabilities request never pays the blocking subprocess cost.
    # Runs in a thread-pool executor via the same helper used by both routers.
    from potluck_local.routers.peer import _cached_hardware
    _track(asyncio.create_task(_cached_hardware()))

    # Fire-and-forget: backfill embeddings for any memories that don't have them.
    # Runs to completion in the background; never blocks startup or the event loop.
    _track(asyncio.create_task(run_embedding_backfill()))

    # Background model-catalog feed: refresh the cloud catalog once at startup,
    # then every 6h. Writes only a disk cache (never the bundled data file) and
    # never blocks a request - model_registry falls back to bundled if the feed
    # is offline or not yet deployed. See services/catalog_feed.py.
    _track(asyncio.create_task(catalog_refresh_loop()))

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
    _track(asyncio.create_task(_stale_session_sweeper()))

    # The Contribute compute toggle: if it was on when the app last ran, the
    # in-process contributor comes back up with the sidecar. Never fatal.
    try:
        from potluck_local.services.contributor_service import get_contributor_service
        from potluck_local.services.settings_manager import get_settings as _get_app_settings

        if os.name != "nt":
            from potluck_local.services.member_account import get_member_account
            session = get_member_account().session()
            if session:
                get_contributor_service().set_member_session(session["token"])
        await get_contributor_service().apply_settings(_get_app_settings())
    except Exception:  # noqa: BLE001
        logger.exception("contributor_startup_failed")

    # Restore only the last successful local load, without delaying /control
    # readiness for a multi-GB model. Explicit model commands share its gate.
    from potluck_local.services.model_lifecycle import get_model_lifecycle
    from potluck_local.routers.models import _load_model

    _track(asyncio.create_task(get_model_lifecycle().restore(_load_model)))

    from potluck_local.services.batch_jobs import BatchManager
    from potluck_local.utils.paths import data_dir
    try:
        app.state.batch_manager = BatchManager(data_dir() / "batches")
    except Exception:
        # A damaged/private-permission store disables batches, not local chat.
        # Never log raw records or exceptions which could include user content.
        app.state.batch_manager = None
        logger.error("batch_storage_unavailable")

    yield

    from potluck_local.services.split_manager import get_split_manager
    await get_split_manager().close()

    if app.state.batch_manager is not None:
        try:
            await app.state.batch_manager.close()
        except Exception:
            logger.error("batch_shutdown_failed")

    # Stop the contributor first: closing its socket is what tells the
    # coordinator to drain this peer, and it must happen before the model
    # it serves with is unloaded below.
    try:
        from potluck_local.services.contributor_service import get_contributor_service

        await get_contributor_service().stop()
    except Exception:  # noqa: BLE001
        logger.exception("contributor_shutdown_failed")

    # Stop the supervised Codex process before the event loop and trace store
    # begin shutting down. Managed sessions fail closed from the UI's point of
    # view, while Codex remains responsible for terminating its own child work.
    try:
        from potluck_local.services.managed_agents import get_managed_agent_manager

        await get_managed_agent_manager().shutdown()
    except Exception:  # noqa: BLE001
        logger.exception("managed_agents_shutdown_failed")

    # Cancel + drain every background task before touching the model or DB
    # below, so nothing still in flight (e.g. the sweeper mid-sleep, or the
    # embedding backfill mid-batch) can wake up after the loop this
    # lifespan belongs to has started closing.
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    await get_model_lifecycle().stop()

    # Release Metal / llama.cpp before process exit. If the model is still
    # loaded, ggml-metal's atexit finalize can GGML_ASSERT on macOS
    # (seen on Ctrl+C / SIGTERM of `make dev`).
    try:
        from potluck_local.services.runtime import get_runtime

        get_runtime().unload_model()
        gc.collect()
    except Exception:  # noqa: BLE001
        logger.exception("shutdown_unload_failed")


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
    # surface.
    sidecar_token = os.environ.get("POTLUCK_SIDECAR_TOKEN") or None

    # Separate opt-in key for the OpenAI-compatible gateway (/v1/*), so external
    # tools (Cursor, etc.) authenticate without the internal sidecar token. Unset
    # means: gateway off in production (sidecar token set), open on loopback in
    # dev. Enforced in peer_access_middleware below.
    gateway_key = os.environ.get("POTLUCK_GATEWAY_API_KEY") or None

    # Mesh CIDR - the trust boundary /peer/* enforces (see _resolve_mesh_network
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

    from potluck_local.services.batch_jobs import BatchError

    @app.exception_handler(BatchError)
    async def batch_error_handler(request: Request, error: BatchError):
        return JSONResponse(status_code=error.status, content={"detail": str(error), "code": error.code})

    # Peer-access guard - when the sidecar is bound to 0.0.0.0 (required so
    # household peers can dial it over the mesh), the bare socket would
    # expose every endpoint. This middleware restricts non-loopback callers
    # to the explicit peer-facing surface (/peer/* + /health). /peer/* and the
    # full /health body need a household member: a source address inside the
    # mesh CIDR (mesh_network above) that this machine's own mesh sidecar lists
    # as a reachable peer, on a connection accepted at this machine's own mesh
    # address (services/peer_trust.py). The CIDR alone proves nothing, because
    # Tailscale and some carrier and campus networks use the same range. The
    # local user's memory, tasks, conversation history, and agent
    # observability are never reachable from another machine, even one in
    # the same household, let alone the same coffee shop.
    #
    # Localhost source IPs (Electron, dev tools) get the full surface.
    @app.middleware("http")
    async def peer_access_middleware(request: Request, call_next):
        raw_path = request.url.path
        # Reject null bytes - they can confuse downstream parsers.
        if "\x00" in raw_path:
            return JSONResponse(status_code=400, content={"detail": "invalid path"})
        # Normalise away any ../ traversal sequences once, up front, so both the
        # peer-surface restriction and the token gate match the real target path.
        normalised = posixpath.normpath(raw_path)
        # posixpath.normpath preserves a leading "//"; collapse it so the
        # gateway prefix check and the exempt/peer checks see one canonical path.
        normalised = "/" + normalised.lstrip("/")

        # Trust the socket peer IP, and only the socket peer IP. We deliberately
        # do not honor X-Forwarded-For / X-Real-IP here: the sidecar is dialed
        # directly (Electron over loopback, peers over the raw WireGuard mesh IP),
        # never through a reverse proxy. If a proxy is ever placed in front of the
        # sidecar, every request would present the proxy's loopback IP and get the
        # full local surface - so that must not happen without revisiting this gate.
        client_host = request.client.host if request.client else ""
        is_loopback = client_host in ("127.0.0.1", "::1", "localhost")

        # Non-loopback callers (household peers over the mesh) are restricted to
        # the explicit peer-facing surface (/peer/* + /health). The local user's
        # memory, tasks, conversation history, and agent observability are never
        # reachable from another machine, even one in the same household.
        if not is_loopback:
            if not (normalised.startswith("/peer/") or normalised == "/health"):
                return _refusal(
                    normalised, 403,
                    "this path is only available to localhost; "
                    "household peers may call /peer/* or /health",
                )

            # /peer/* and the full /health body are for household members only
            # (see the guard comment above). Both addresses come from the TCP
            # socket: scope["client"] is the source and scope["server"] is the
            # local address the connection was accepted on. Neither is read
            # from a header. Anything the check cannot establish is a refusal.
            member = False
            if _client_ip_in_network(client_host, mesh_network):
                server = request.scope.get("server")
                server_host = server[0] if isinstance(server, (tuple, list)) and len(server) == 2 else None
                reason = await peer_trust.household_roster().refusal_reason(client_host, server_host)
                member = reason is None
            else:
                reason = "outside_mesh_range"
            if not member and (normalised.startswith("/peer/") or normalised == "/health"):
                # Reason code and source only, at debug level, so a stranger
                # cannot fill the log.
                logger.debug("peer_refused reason=%s source=%s", reason, client_host)
            if normalised.startswith("/peer/") and not member:
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": (
                            "peer routes are only available to machines in "
                            "this household's mesh"
                        ),
                    },
                )
            # Liveness stays open to any caller, but only a member or the local
            # user gets the version, loaded model and other machines' addresses.
            if normalised == "/health" and not member and request.method == "GET":
                return JSONResponse(status_code=200, content={"status": "ok"})

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
                # The key is a bearer token (OpenAI clients) or an x-api-key header
                # (the Anthropic SDKs). Either one may carry it.
                auth = request.headers.get("authorization", "")
                bearer = auth[7:] if auth[:7].lower() == "bearer " else ""
                if not (
                    _key_matches(bearer, effective_key)
                    or _key_matches(request.headers.get("x-api-key", ""), effective_key)
                ):
                    return _refusal(normalised, 401, "missing or invalid gateway API key")
                return await call_next(request)
            # Gateway not configured: open only when the sidecar gate is off (dev).
            # In production (token set) it stays disabled until the user enables it.
            if sidecar_token is not None:
                return _refusal(normalised, 404, "gateway disabled; enable it in Settings -> Connect tools")
            return await call_next(request)

        # Shared-secret gate - applies to loopback callers too. When a token is
        # configured, every non-exempt path must present a matching bearer token.
        # This is what stops a malicious local process from reading the user's
        # data over 127.0.0.1. Exempt: liveness (/health) and /peer/* -
        # but only for a non-loopback caller.
        #
        # NOTE on /peer/* exemption: peer routes are not per-request signature
        # authenticated. Their credential is household membership: a
        # caller reaches them only from a source address that this machine's
        # mesh sidecar lists as a reachable peer, on a connection accepted at
        # this machine's own mesh address (Gate 1 above 403s any non-loopback
        # path outside /peer/* + /health, and the membership check just above
        # that 403s every other /peer/* caller), and mesh enrollment is gated by
        # the coordinator. TODO: per-request signed-nonce attestation. See
        # routers/peer.py "Trust model".
        #
        # That membership credential only exists for a non-loopback caller:
        # the check above runs exclusively inside `if not is_loopback:`
        # (Gate 1), so a loopback caller hitting /peer/* never proves
        # membership at all. Exempting /peer/* from the token gate
        # unconditionally would leave it completely unauthenticated for any
        # local process (no token, or the wrong token, would both work) and
        # leak private memory. A loopback /peer/* caller has no mesh
        # credential to fall back on, so it must present the sidecar token like
        # every other loopback route; a mesh peer keeps its token-free path
        # since household membership already gated it above.
        if normalised.startswith(("/account", "/batches", "/split", "/circles")) and sidecar_token is None:
            return JSONResponse(status_code=503, content={"detail": "Private control requires an authenticated runtime. Run potluck up."})
        if sidecar_token is not None:
            exempt = (
                normalised == "/health"
                or (normalised.startswith("/peer/") and not is_loopback)
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

    # Routers - prefixes must match localApi.ts in the frontend
    @app.exception_handler(SplitError)
    async def split_error_handler(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)},
                            headers={"X-Potluck-Error": exc.code})

    app.include_router(split.router, prefix="/split", tags=["split"])
    app.include_router(split.peer_router, prefix="/peer/split", tags=["split"])
    app.include_router(health.router, tags=["health"])
    app.include_router(account.router, prefix="/account", tags=["account"])
    app.include_router(circles.router, prefix="/circles", tags=["circles"])
    app.include_router(batches.router, prefix="/batches", tags=["batches"])
    app.include_router(readiness.router, tags=["readiness"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(chat_circle.router, prefix="/chat", tags=["chat"])
    app.include_router(models.router, prefix="/models", tags=["models"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(hardware.router, prefix="/hardware", tags=["hardware"])
    app.include_router(memory.router, prefix="/memory", tags=["memory"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    app.include_router(agents.router, prefix="/agents", tags=["agents"])
    app.include_router(usage.router, prefix="/usage", tags=["usage"])
    app.include_router(vault.router, prefix="/vault", tags=["vault"])
    app.include_router(peer.router, prefix="/peer", tags=["peer"])
    app.include_router(contributor.router, prefix="/contributor", tags=["contributor"])
    app.include_router(verify.router, prefix="/verify", tags=["verify"])
    app.include_router(gateway.router, prefix="/v1", tags=["gateway"])
    app.include_router(anthropic.router, prefix="/v1", tags=["gateway"])
    app.include_router(ollama.router, prefix="/api", tags=["ollama"])

    return app


def main() -> None:
    """Start the local engine, optionally publishing private shared control."""
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--split-supervisor":
        from potluck_local.services.native_supervisor import run
        raise SystemExit(run(sys.argv[2:]))
    if len(sys.argv) == 3 and sys.argv[1] == "--gpu-probe":
        from potluck_local.services.gpu_probe import main as probe
        probe(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description="Potluck local sidecar")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("POTLUCK_PORT", "8321")),
        help="Port to listen on (default 8321)",
    )
    parser.add_argument(
        "--probe-inference-backend",
        metavar="LIB_DIR",
        default=None,
        help=(
            "Load the llama.cpp backend set in LIB_DIR, print what it reports as "
            "JSON, and exit. The sidecar re-invokes itself this way so a GPU "
            "backend that cannot load takes down a throwaway process instead of "
            "the sidecar."
        ),
    )
    args = parser.parse_args()

    if args.probe_inference_backend:
        import json as _json
        from pathlib import Path as _Path

        from potluck_local.services.llama_backend import probe_lib_dir

        print(_json.dumps(probe_lib_dir(_Path(args.probe_inference_backend))))
        return

    dev_mode = os.environ.get("POTLUCK_DEV", "0") == "1"
    setup_logging(dev=dev_mode)

    # Decide which llama.cpp backend set this process loads, before anything
    # imports llama_cpp (the library resolves its native library at import
    # time). On platforms that ship a single backend set this is a no-op.
    from potluck_local.services.llama_backend import select_backend

    selection = select_backend()
    logger.info(
        "inference_backend backend=%s lib_dir=%s probed=%s reason=%s",
        selection.backend,
        selection.lib_dir or "(wheel default)",
        selection.probed,
        selection.reason,
    )

    app = create_app()
    # Bind to all interfaces so household-mesh peers can dial us over the
    # WireGuard mesh IP. The peer_access_middleware in create_app() is the
    # access boundary - non-loopback callers can only reach /peer/* + /health,
    # not the local user's memory / tasks / chat history. Override with
    # POTLUCK_BIND_HOST=127.0.0.1 to opt out (e.g. on a machine that should
    # never serve peer inference).
    bind_host = os.environ.get("POTLUCK_BIND_HOST", "0.0.0.0")
    # proxy_headers is off on both servers below. uvicorn's default rewrites the
    # caller's address from X-Forwarded-For when the connection comes from
    # 127.0.0.1, which would let a local process pose as a mesh peer and skip
    # the token that loopback /peer/* requires. Nothing proxies to the engine.
    if os.environ.get("POTLUCK_SHARED_RUNTIME") == "1":
        from potluck_local.runtime_control import RuntimeControl, cleanup_on_sigterm

        with cleanup_on_sigterm(), RuntimeControl(port=args.port, host=bind_host,
                            token=os.environ.get("POTLUCK_SIDECAR_TOKEN", ""),
                            owner=os.environ.get("POTLUCK_RUNTIME_OWNER", "standalone")) as control:
            app.state.runtime_control = control
            server = uvicorn.Server(uvicorn.Config(
                app, host=bind_host, port=control.port, proxy_headers=False,
                log_level="debug" if dev_mode else "info"))
            app.state.runtime_server = server
            server.run(sockets=[control.socket])
        return
    uvicorn.run(
        app,
        host=bind_host,
        port=args.port,
        proxy_headers=False,
        log_level="debug" if dev_mode else "info",
    )


if __name__ == "__main__":
    main()
