"""Binds and runs the circle listener as a second server inside the engine.

The circle listener (routers/circle_link.py) is a separate minimal app on a port
of its own, default 8340 (POTLUCK_CIRCLE_PORT). The engine's main port does
not carry it, so nothing of the household surface can be reached through it.
This module owns only its life as a server:

  * bind to this machine's mesh address and nothing else. A machine on the same
    LAN cannot connect to the listener at all, let alone hold its connections
    open; only traffic that arrives through the mesh interface reaches it;
  * find a free port, starting at the wanted one and counting up when it is
    busy, and report whichever port was bound;
  * run a second uvicorn server on the engine's event loop without taking over
    the process's signal handlers (the main server owns SIGINT and SIGTERM);
  * stop it, bounded in time, even with a request still streaming.

Nothing here decides *whether* the listener should run. The contributor service
does that (it runs only while this machine shares with circles, the coordinator
brokers links and the mesh sidecar advertises circle_link_v1), and a failure to
bind or start is reported to it as ListenerError. It must never reach the
contributor loop, the relay path or engine startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import logging
import os
import socket
from collections.abc import Iterator

import uvicorn
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8340
PORT_ENV = "POTLUCK_CIRCLE_PORT"
# How many ports are tried, counting up from the wanted one.
PORT_TRIES = 32
START_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 5.0
# A running request is cut after this long when the server stops.
GRACEFUL_SHUTDOWN_S = 2


class ListenerError(RuntimeError):
    """The listener could not be bound or started."""


class AddressNotAvailable(ListenerError):
    """This machine does not carry the address asked for (yet): the mesh interface
    may still be coming up. Every port would fail the same way, so none are tried."""


def wanted_port() -> int:
    """POTLUCK_CIRCLE_PORT, or the default when it is unset or not a port."""
    raw = os.environ.get(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        logger.warning("circle_port_invalid using_default=%d", DEFAULT_PORT)
        return DEFAULT_PORT
    return port


def bind_host() -> str:
    """The address the engine's main server binds (POTLUCK_BIND_HOST). The circle
    listener binds the mesh address instead; this is read only to see whether the
    owner limited the engine to this machine, which means it serves no one else."""
    return os.environ.get("POTLUCK_BIND_HOST", "0.0.0.0")


def host_is_loopback(host: str) -> bool:
    """True for a bind address that only this machine can reach. A machine
    started with POTLUCK_BIND_HOST=127.0.0.1 has said it serves no one else."""
    if host == "localhost":
        return True
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_listening_socket(host: str, port: int, tries: int = PORT_TRIES) -> socket.socket:
    """A bound, non-blocking socket on the first free port from `port` up.

    Raises ListenerError when none of `tries` ports could be bound.
    """
    last_error: OSError | None = None
    for candidate in range(port, min(port + tries, 65536)):
        try:
            family, _, _, _, address = socket.getaddrinfo(
                host, candidate, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
            )[0]
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            raise ListenerError(f"cannot create a socket for {host}: {type(exc).__name__}") from exc
        try:
            if os.name == "nt":
                # SO_REUSEADDR on Windows lets a second program share a port that is
                # in use, which would hide a busy port. Ask for the port exclusively.
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(address)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRNOTAVAIL:
                raise AddressNotAvailable(f"{host} is not an address of this machine") from exc
            last_error = exc
    raise ListenerError(
        f"no free port from {port} to {min(port + tries, 65536) - 1}"
        + (f" ({type(last_error).__name__})" if last_error else "")
    )


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone.

    uvicorn installs SIGINT and SIGTERM handlers while a server runs and puts the
    old ones back when it stops. The engine's main server owns those signals; a
    second server that took them over when the listener started would make a
    SIGTERM stop only the listener.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class CircleListener:
    """One circle listener: a bound socket and a server on the running loop."""

    def __init__(self, app: ASGIApp, *, host: str, port: int | None = None) -> None:
        if not host or ipaddress.ip_address(host).is_unspecified:
            # Never every interface: the caller names the mesh address.
            raise ValueError("the circle listener needs the mesh address to bind, not a wildcard")
        self._app = app
        self._host = host
        self._port_wanted = port
        self._server: _EmbeddedServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._sock: socket.socket | None = None
        self._port: int | None = None
        self._stopping = False

    @property
    def host(self) -> str:
        """The address the listener binds (and, once started, is bound to)."""
        return self._host

    @property
    def port(self) -> int | None:
        """The port the listener is bound to, or None when it is not running."""
        return self._port if self.running else None

    @property
    def running(self) -> bool:
        server, task = self._server, self._task
        return bool(server is not None and task is not None and not task.done() and server.started)

    async def start(self) -> int:
        """Bind and start. Returns the bound port; raises ListenerError."""
        if self.running and self._port is not None:
            return self._port
        wanted = self._port_wanted if self._port_wanted is not None else wanted_port()
        sock = bind_listening_socket(self._host, wanted)
        config = uvicorn.Config(
            self._app,
            log_config=None,
            log_level="warning",
            access_log=False,
            # The caller's address is the socket's, never X-Forwarded-For: the gate
            # decides on it, and nothing proxies to this listener.
            proxy_headers=False,
            # Not lifespan="off": that module is not in the packaged build's hidden
            # imports. The default ("auto") uses the one the main server uses.
            ws="none",
            server_header=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
            limit_concurrency=128,
        )
        server = _EmbeddedServer(config)
        task = asyncio.create_task(server.serve(sockets=[sock]), name="potluck-circle-listener")
        try:
            async with asyncio.timeout(START_TIMEOUT_S):
                while not server.started:
                    if task.done():
                        reason = type(task.exception()).__name__ if not task.cancelled() and task.exception() else "exited"
                        raise ListenerError(f"listener did not start ({reason})")
                    await asyncio.sleep(0.01)
        except BaseException as exc:
            server.should_exit = True
            server.force_exit = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sock.close()
            if isinstance(exc, (ListenerError, asyncio.CancelledError)):
                raise
            raise ListenerError(f"listener did not start ({type(exc).__name__})") from exc
        self._server, self._task, self._sock = server, task, sock
        self._port = sock.getsockname()[1]
        self._stopping = False
        task.add_done_callback(self._on_done)
        logger.info("circle_listener_started port=%d", self._port)
        return self._port

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if self._stopping or task.cancelled():
            return
        error = task.exception()
        logger.error(
            "circle_listener_stopped_unexpectedly error=%s", type(error).__name__ if error else "exited"
        )

    async def stop(self) -> None:
        """Stop serving and release the port. Safe to call when not running."""
        server, task, sock = self._server, self._task, self._sock
        self._server = self._task = self._sock = None
        port, self._port = self._port, None
        self._stopping = True
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                async with asyncio.timeout(STOP_TIMEOUT_S):
                    await asyncio.shield(task)
            except TimeoutError:
                logger.warning("circle_listener_stop_timeout; cancelling")
                if server is not None:
                    server.force_exit = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        if port is not None:
            logger.info("circle_listener_stopped port=%d", port)
