"""Who may have created the mesh sidecar's socket.

On macOS the socket is /tmp/potluck-mesh.sock, a directory every local user can
write to, so another user could put their own socket there and feed the engine
a roster of their choosing. Where the directory is writable by group or others
the engine therefore requires the socket to be owned by root or by its own
user. Where only the owner can write (/run on Linux, where the packaged daemon
owns its socket as the `potluck` service user and grants the desktop user
access with an ACL, or an XDG runtime directory) any owner is accepted. In both
cases the path must be a socket, and a symlink must not lead around the rule.

These tests use real sockets and real directories. Another owner is simulated
by changing which uid the engine believes it has, since a test cannot chown.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import stat
import tempfile

import pytest

from potluck_local.services import mesh_ipc
from potluck_local.services.mesh_ipc import MeshUnavailable, mesh_get

pytestmark = pytest.mark.skipif(os.name == "nt", reason="unix sockets only")

OTHER_UID = os.geteuid() + 1000


@pytest.fixture()
def base():
    """A short directory (a unix socket path is limited to about 100 bytes)."""
    path = tempfile.mkdtemp(prefix="pk-")
    os.chmod(path, 0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_dir(base: str, name: str, mode: int) -> str:
    path = os.path.join(base, name)
    os.mkdir(path)
    os.chmod(path, mode)
    return path


def make_socket(directory: str, name: str = "m.sock") -> tuple[str, socket.socket]:
    path = os.path.join(directory, name)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen(1)
    return path, sock


@pytest.fixture()
def sockets():
    opened: list[socket.socket] = []
    yield opened
    for s in opened:
        s.close()


def made(directory: str, sockets, name: str = "m.sock") -> str:
    path, sock = make_socket(directory, name)
    sockets.append(sock)
    return path


def check(path: str) -> None:
    mesh_ipc._require_trusted_unix_socket(path)


def foreign(monkeypatch) -> None:
    """Make every file we created look as if another user owns it."""
    monkeypatch.setattr(mesh_ipc.os, "geteuid", lambda: OTHER_UID)


def as_owned_by_root(monkeypatch, path: str) -> None:
    real_stat = os.stat
    target = os.path.realpath(path)

    def fake_stat(p, *args, **kwargs):
        result = real_stat(p, *args, **kwargs)
        if os.path.realpath(p) == target:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(mesh_ipc.os, "stat", fake_stat)


# --- a directory others can write to (the macOS shape) -----------------------


@pytest.mark.parametrize("mode", [0o1777, 0o777, 0o770, 0o722, 0o702])
def test_shared_directory_accepts_a_socket_the_engine_user_owns(base, sockets, mode):
    shared = make_dir(base, "shared", mode)
    check(made(shared, sockets))


@pytest.mark.parametrize("mode", [0o1777, 0o777, 0o770, 0o720, 0o702])
def test_shared_directory_refuses_a_socket_another_user_owns(base, sockets, monkeypatch, mode):
    shared = make_dir(base, "shared", mode)
    path = made(shared, sockets)
    foreign(monkeypatch)
    with pytest.raises(MeshUnavailable, match="another owner"):
        check(path)


def test_shared_directory_accepts_a_root_owned_socket(base, sockets, monkeypatch):
    # The macOS daemon runs as root and leaves its socket root-owned when no
    # user is logged in at the console.
    shared = make_dir(base, "shared", 0o1777)
    path = made(shared, sockets)
    foreign(monkeypatch)
    as_owned_by_root(monkeypatch, path)
    check(path)


# --- a directory only its owner can write (the Linux shape) ------------------


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o700, 0o555])
def test_private_directory_accepts_any_owner(base, sockets, monkeypatch, mode):
    run = make_dir(base, "run", 0o755)
    path = made(run, sockets)
    os.chmod(run, mode)
    foreign(monkeypatch)
    check(path)


# --- it must be a socket ------------------------------------------------------


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_a_regular_file_is_refused(base, mode):
    directory = make_dir(base, "d", mode)
    path = os.path.join(directory, "m.sock")
    with open(path, "w") as f:
        f.write("{}")
    with pytest.raises(MeshUnavailable, match="not a socket"):
        check(path)


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_a_directory_or_pipe_is_refused(base, mode):
    directory = make_dir(base, "d", mode)
    os.mkdir(os.path.join(directory, "m.sock"))
    with pytest.raises(MeshUnavailable, match="not a socket"):
        check(os.path.join(directory, "m.sock"))
    fifo = os.path.join(directory, "fifo")
    os.mkfifo(fifo)
    with pytest.raises(MeshUnavailable, match="not a socket"):
        check(fifo)


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_a_missing_path_is_refused(base, mode):
    directory = make_dir(base, "d", mode)
    with pytest.raises(MeshUnavailable):
        check(os.path.join(directory, "absent.sock"))
    with pytest.raises(MeshUnavailable):
        check(os.path.join(base, "no-such-dir", "m.sock"))


# --- symlinks -----------------------------------------------------------------


def test_a_symlink_in_a_shared_directory_is_refused_even_to_a_good_socket(base, sockets):
    # Anyone can plant a link in /tmp. The daemon never creates one.
    private = make_dir(base, "private", 0o755)
    target = made(private, sockets)
    shared = make_dir(base, "shared", 0o1777)
    link = os.path.join(shared, "m.sock")
    os.symlink(target, link)
    with pytest.raises(MeshUnavailable, match="symlink"):
        check(link)


def test_a_link_from_a_private_directory_to_a_foreign_shared_socket_is_refused(base, sockets, monkeypatch):
    # The target is judged by its own directory, so a link cannot launder it.
    shared = make_dir(base, "shared", 0o1777)
    target = made(shared, sockets)
    private = make_dir(base, "private", 0o755)
    link = os.path.join(private, "m.sock")
    os.symlink(target, link)
    check(link)  # ours: fine
    foreign(monkeypatch)
    with pytest.raises(MeshUnavailable, match="another owner"):
        check(link)


def test_a_link_between_private_directories_is_followed(base, sockets, monkeypatch):
    private = make_dir(base, "private", 0o755)
    target = made(private, sockets)
    elsewhere = make_dir(base, "elsewhere", 0o755)
    link = os.path.join(elsewhere, "m.sock")
    os.symlink(target, link)
    foreign(monkeypatch)
    check(link)


def test_a_link_to_a_regular_file_is_refused(base):
    private = make_dir(base, "private", 0o755)
    plain = os.path.join(private, "plain")
    open(plain, "w").close()
    link = os.path.join(private, "m.sock")
    os.symlink(plain, link)
    with pytest.raises(MeshUnavailable, match="not a socket"):
        check(link)


def test_a_symlinked_directory_is_resolved_like_the_real_one(base, sockets, monkeypatch):
    # /tmp is itself a link to /private/tmp on macOS.
    shared = make_dir(base, "shared", 0o1777)
    path = made(shared, sockets)
    alias = os.path.join(base, "alias")
    os.symlink(shared, alias)
    check(os.path.join(alias, "m.sock"))
    foreign(monkeypatch)
    with pytest.raises(MeshUnavailable, match="another owner"):
        check(os.path.join(alias, "m.sock"))
    private = make_dir(base, "private", 0o755)
    private_path = made(private, sockets, "p.sock")
    alias2 = os.path.join(base, "alias2")
    os.symlink(private, alias2)
    check(os.path.join(alias2, "p.sock"))
    assert path and private_path


def test_a_relative_path_is_checked_against_the_working_directory(base, sockets, monkeypatch):
    shared = make_dir(base, "shared", 0o1777)
    made(shared, sockets)
    monkeypatch.chdir(shared)
    check("m.sock")
    foreign(monkeypatch)
    with pytest.raises(MeshUnavailable, match="another owner"):
        check("m.sock")


# --- through mesh_get, against a real listener --------------------------------


async def serve(path: str, body: object):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        raw = json.dumps(body).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(raw)).encode() + b"\r\nConnection: close\r\n\r\n" + raw)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=path)


def run_get(monkeypatch, path: str, expect: object | None = None):
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", path)

    async def go():
        server = await serve(path, expect or {"mesh_ip": "100.64.0.4"})
        try:
            return await mesh_get("/status")
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(go())


@pytest.mark.parametrize("mode", [0o755, 0o1777])
def test_mesh_get_reads_from_a_socket_the_engine_user_owns(base, monkeypatch, mode):
    directory = make_dir(base, "d", mode)
    assert run_get(monkeypatch, os.path.join(directory, "m.sock")) == {"mesh_ip": "100.64.0.4"}


def test_mesh_get_refuses_a_foreign_socket_in_a_shared_directory(base, monkeypatch):
    shared = make_dir(base, "shared", 0o1777)
    path = os.path.join(shared, "m.sock")
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", path)

    async def go():
        server = await serve(path, {"mesh_ip": "100.64.0.99"})
        foreign(monkeypatch)
        try:
            with pytest.raises(MeshUnavailable, match="another owner"):
                await mesh_get("/status")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(go())


def test_mesh_get_with_no_socket_still_says_the_sidecar_is_not_running(base, monkeypatch):
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", os.path.join(base, "absent.sock"))
    with pytest.raises(MeshUnavailable, match="not running"):
        asyncio.run(mesh_get("/status"))


def test_the_real_socket_modes_the_sidecar_creates_pass(base, sockets):
    # listener_unix.go chmods the socket to 0600 and may chown it to the
    # desktop user; the check looks at the directory and the owner, not the mode.
    shared = make_dir(base, "shared", 0o1777)
    path = made(shared, sockets)
    os.chmod(path, 0o600)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    check(path)
