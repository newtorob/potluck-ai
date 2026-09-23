"""Shared runtime credentials/ownership, exercised without starting model services."""
import json
import os
import socket
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from potluck_local.runtime_control import RuntimeControl

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shared runtime protocol")
TOKEN = "a" * 64


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    return tmp_path


def runtime(**kwargs):
    return RuntimeControl(port=0, host="127.0.0.1", token=TOKEN, owner="cli", **kwargs)


def test_private_record_and_cleanup(isolated):
    with runtime() as control:
        saved = json.loads(control.path.read_text())
        assert saved == {**control.info(), "token": TOKEN}
        assert control.port > 0
        assert control.path.stat().st_mode & 0o777 == 0o600
        assert control.directory.stat().st_mode & 0o777 == 0o700
        assert "token" not in control.info()
    assert not control.path.exists()


@pytest.mark.parametrize("sent_signal", [signal.SIGTERM, signal.SIGINT])
def test_uvicorn_signal_shutdown_removes_discovery(isolated, sent_signal):
    """Exercise Uvicorn's real signal replay, not an HTTP shutdown mock."""
    code = """
import uvicorn
from fastapi import FastAPI
from potluck_local.runtime_control import RuntimeControl, cleanup_on_sigterm
with cleanup_on_sigterm(), RuntimeControl(port=0, host='127.0.0.1', token='a'*64, owner='standalone') as control:
    uvicorn.Server(uvicorn.Config(FastAPI(), log_level='error')).run(sockets=[control.socket])
"""
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        record = isolated / "run/runtime.json"
        for _ in range(100):
            if record.exists():
                port = int(json.loads(record.read_text())["url"].rsplit(":", 1)[1])
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1) as connection:
                        connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                        if connection.recv(1024):
                            break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            pytest.fail("Fixture server did not start")
        child.send_signal(sent_signal)
        child.wait(timeout=10)
        assert not record.exists(), "Signal shutdown left stale discovery credentials"
        with runtime() as restarted:
            assert restarted.port > 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_second_launcher_cannot_replace_live_record(isolated):
    with runtime() as first:
        saved = first.path.read_bytes()
        with pytest.raises(RuntimeError, match="already owns"):
            with runtime():
                pytest.fail("Duplicate runtime started")
        assert first.path.read_bytes() == saved
    with runtime() as second:
        assert second.instance_id != first.instance_id


def test_stale_record_replaced_only_after_port_bound(isolated):
    directory = isolated / "run"
    directory.mkdir(mode=0o700)
    record = directory / "runtime.json"
    record.write_text('{"instanceId":"stale"}')
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(OSError):
            with RuntimeControl(port=busy.getsockname()[1], host="127.0.0.1", token=TOKEN, owner="cli"):
                pytest.fail("Bound an occupied port")
    assert json.loads(record.read_text())["instanceId"] == "stale"
    with runtime() as control:
        assert json.loads(record.read_text())["instanceId"] == control.instance_id


def test_symlink_directory_and_lock_rejected(isolated):
    target = isolated / "target"
    target.mkdir()
    (isolated / "run").symlink_to(target, target_is_directory=True)
    with pytest.raises(PermissionError):
        with runtime():
            pass
    (isolated / "run").unlink()
    (isolated / "run").mkdir(mode=0o700)
    (isolated / "run" / "runtime.lock").symlink_to(target / "other")
    with pytest.raises(OSError):
        with runtime():
            pass
    assert not (target / "other").exists()


def test_control_and_shutdown_require_control_token(isolated, monkeypatch):
    from potluck_local.app import create_app
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", TOKEN)
    app = create_app()
    app.state.runtime_control = SimpleNamespace(info=lambda: {"schemaVersion": 1, "instanceId": "test"})
    client = TestClient(app, client=("127.0.0.1", 1234))
    for method, path in [("GET", "/control"), ("POST", "/shutdown")]:
        assert client.request(method, path).status_code == 401
        assert client.request(method, path, headers={"Authorization": "Bearer gateway-key"}).status_code == 401
    assert client.get("/control", headers={"Authorization": f"Bearer {TOKEN}"}).json() == {
        "schemaVersion": 1, "instanceId": "test"}
    peer = TestClient(app, client=("100.64.0.9", 1234))
    assert peer.get("/control", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 403


def test_main_passes_bound_socket_and_identity_to_uvicorn(isolated, monkeypatch):
    from potluck_local import app as module
    monkeypatch.setenv("POTLUCK_SHARED_RUNTIME", "1")
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", TOKEN)
    monkeypatch.setenv("POTLUCK_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("POTLUCK_RUNTIME_OWNER", "cli")
    monkeypatch.setattr("sys.argv", ["potluck-local", "--port", "0"])
    seen = []
    class Server:
        def __init__(self, config):
            self.config = config
        def run(self, sockets):
            control = self.config.app.state.runtime_control
            assert sockets[0].getsockname()[1] == control.port
            assert control.path.exists()
            seen.append(control)
    monkeypatch.setattr(module.uvicorn, "Server", Server)
    module.main()
    assert len(seen) == 1
    assert not seen[0].path.exists()
