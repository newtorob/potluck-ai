"""Household routing discovers the same daemon as the installed CLI."""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from potluck_local.services import gateway_routing as routing


@pytest.fixture
def socket_dir():
    # macOS's pytest temp root can exceed the AF_UNIX path length limit.
    with tempfile.TemporaryDirectory(prefix="pl-mesh-", dir="/tmp") as directory:
        yield Path(directory)


def test_explicit_socket_reads_real_roster_and_does_not_fall_back(monkeypatch, socket_dir):
    socket = socket_dir / "mesh.sock"
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", str(socket))

    async def exercise():
        async def handle(reader, writer):
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                assert request.startswith(b"GET /peers ")
                body = json.dumps({"peers": [
                    {"node_name": "available", "mesh_ip": "100.64.1.2", "reachable": True},
                    {"node_name": "offline", "mesh_ip": "100.64.1.3", "reachable": False},
                ]}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_unix_server(handle, str(socket))
        async with server:
            assert await routing.household_peers() == [{"node_name": "available", "mesh_ip": "100.64.1.2"}]
        monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", str(socket_dir / "missing.sock"))
        assert await routing.household_peers() == []

    asyncio.run(exercise())


def test_linux_packaged_daemon_takes_precedence_over_user_socket(monkeypatch):
    monkeypatch.setattr(routing.sys, "platform", "linux")
    monkeypatch.setattr(routing.platform, "system", lambda: "Linux")
    monkeypatch.delenv("POTLUCK_LOCAL_API_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    monkeypatch.setattr(routing.os.path, "exists", lambda path: path == "/run/potluck-mesh.sock")
    assert routing.mesh_socket_path() == "/run/potluck-mesh.sock"
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", "/custom/mesh.sock")
    assert routing.mesh_socket_path() == "/custom/mesh.sock"


def test_user_socket_remains_fallback_and_windows_stays_unsupported(monkeypatch):
    monkeypatch.setattr(routing.sys, "platform", "linux")
    monkeypatch.setattr(routing.platform, "system", lambda: "Linux")
    monkeypatch.delenv("POTLUCK_LOCAL_API_SOCKET", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    monkeypatch.setattr(routing.os.path, "exists", lambda _: False)
    assert routing.mesh_socket_path() == "/run/user/1234/potluck-mesh.sock"
    monkeypatch.setattr(routing.sys, "platform", "win32")
    monkeypatch.setenv("POTLUCK_LOCAL_API_SOCKET", "a-named-pipe")
    assert routing.mesh_socket_path() is None
