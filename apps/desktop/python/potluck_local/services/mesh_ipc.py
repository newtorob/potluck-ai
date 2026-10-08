"""Read-only calls to this machine's own mesh sidecar over its local socket.

The Go mesh sidecar serves a small HTTP API on a unix socket (macOS, Linux) or
a named pipe (Windows). Only processes on this machine can reach it. The engine
reads two things from it: `/status` (this machine's own mesh address) and
`/peers` (the other machines in this household).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import stat
import sys
import threading
from typing import Any

import httpx

from . import mesh_pipe
from .gateway_routing import mesh_socket_path

DEFAULT_TIMEOUT_S = 2.0


class MeshUnavailable(Exception):
    """The mesh sidecar could not be asked, or did not give a usable answer."""


# Windows: the sidecar's named pipe is read in a worker thread (mesh_pipe).
# That code polls against a deadline, so a worker ends by itself within
# `timeout` and closes its own handle. This limit is the backstop for a worker
# stuck inside a Windows call: past it, callers fail at once instead of
# starting more threads.
_PIPE_MAX_IN_FLIGHT = 8
_pipe_in_flight = 0
_pipe_lock = threading.Lock()


def _is_windows() -> bool:
    # Same test as mesh_socket_path() in gateway_routing, which returns None here.
    return sys.platform == "win32" or platform.system() == "Windows"


def _pipe_request(path: str, timeout: float) -> tuple[int, bytes]:
    """One GET over the named pipe. Runs in a worker thread."""
    global _pipe_in_flight
    with _pipe_lock:
        if _pipe_in_flight >= _PIPE_MAX_IN_FLIGHT:
            raise mesh_pipe.MeshPipeError("too many mesh pipe requests in flight")
        _pipe_in_flight += 1
    try:
        return mesh_pipe.pipe_get(path, timeout=timeout)
    finally:
        with _pipe_lock:
            _pipe_in_flight -= 1


async def _mesh_get_pipe(path: str, timeout: float) -> Any:
    """The Windows half of mesh_get: same contract, over the named pipe."""
    try:
        mesh_pipe.validate_path(path)
        # The worker stops at its own deadline. wait_for is the caller's hard
        # bound if it does not: the await ends on time and the thread is left to finish.
        status, body = await asyncio.wait_for(asyncio.to_thread(_pipe_request, path, timeout), timeout)
    except mesh_pipe.MeshPipeError as exc:
        raise MeshUnavailable(f"mesh sidecar request failed: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise MeshUnavailable("mesh sidecar request timed out") from exc
    except Exception as exc:
        raise MeshUnavailable(f"mesh sidecar request failed: {type(exc).__name__}") from exc
    if status != 200:
        raise MeshUnavailable(f"mesh sidecar answered HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise MeshUnavailable(f"mesh sidecar request failed: {type(exc).__name__}") from exc


def _require_trusted_unix_socket(sock: str) -> None:
    """Raise MeshUnavailable unless the socket at `sock` is safe to dial.

    It must be a socket. Where another user could have created the path (the
    directory is writable by group or others, like /tmp on macOS) it must be
    owned by root or by this process's own user, and it must not be a symlink
    sitting in that directory. A directory only its owner can write (/run on
    Linux, an XDG runtime dir) needs no owner check, because the packaged Linux
    daemon owns its socket as a service user. The rule is applied to the path
    as given and again to the real path it resolves to, so a link cannot lead
    the engine to a socket the rule would refuse.
    """
    if os.name == "nt":  # a named pipe, not a unix socket
        return
    try:
        parent = os.path.realpath(os.path.dirname(sock) or ".")
        entry = os.path.join(parent, os.path.basename(sock))
        shared = os.stat(parent).st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if shared and stat.S_ISLNK(os.lstat(entry).st_mode):
            raise MeshUnavailable("mesh socket path is a symlink in a shared directory")
        real = os.path.realpath(entry)
        info = os.stat(real)
        if not stat.S_ISSOCK(info.st_mode):
            raise MeshUnavailable("mesh socket path is not a socket")
        shared = shared or os.stat(os.path.dirname(real)).st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if shared and info.st_uid not in (0, os.geteuid()):
            raise MeshUnavailable("mesh socket in a shared directory has another owner")
    except MeshUnavailable:
        raise
    except OSError as exc:
        raise MeshUnavailable(f"mesh socket cannot be checked: {type(exc).__name__}") from exc


async def mesh_get(path: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    """GET `path` (for example "/peers") and return the decoded JSON body.

    Raises MeshUnavailable for every failure: no sidecar, no socket, a refused
    or timed-out connection, a status other than 200, or a body that is not
    JSON. Callers that make a trust decision treat that as "nothing is known".
    """
    if _is_windows():
        return await _mesh_get_pipe(path, timeout)
    sock = mesh_socket_path()
    if not sock:
        raise MeshUnavailable("no local mesh socket on this platform")
    if not os.path.exists(sock):
        raise MeshUnavailable("mesh sidecar is not running")
    _require_trusted_unix_socket(sock)
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            response = await client.get(f"http://localhost{path}")
        if response.status_code != 200:
            raise MeshUnavailable(f"mesh sidecar answered HTTP {response.status_code}")
        return response.json()
    except MeshUnavailable:
        raise
    except Exception as exc:
        raise MeshUnavailable(f"mesh sidecar request failed: {type(exc).__name__}") from exc
