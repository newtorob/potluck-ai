"""Opt-in smoke: built CLI + real local engine + cloud API + isolated Postgres.

Build apps/cli first, then run with POTLUCK_RUN_CLI_SMOKE=1 and
POTLUCK_API_TEST_DB=potluck_test_cli_device. No real email or user state is used.
"""
import asyncio
import json
import os
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn

from potluck_api.app import create_app
from potluck_api.db.database import get_db
from potluck_api.services.email_service import EmailService

pytestmark = pytest.mark.skipif(os.environ.get("POTLUCK_RUN_CLI_SMOKE") != "1", reason="requires built CLI and local runtime venv")
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_ssh_cli_login_restart_logout(client, sessionmaker_, monkeypatch, tmp_path):
    messages = []
    async def capture(_self, email, link):
        messages.append((email, link))
    monkeypatch.setattr(EmailService, "send_device_approval", capture)
    app = create_app()
    async def db_override():
        async with sessionmaker_() as session:
            yield session
    app.dependency_overrides[get_db] = db_override
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    api_port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        engine_port = reserved.getsockname()[1]
    env = {**os.environ, "POTLUCK_DATA_DIR": str(tmp_path), "POTLUCK_CLOUD_API_URL": f"http://127.0.0.1:{api_port}",
           "POTLUCK_SIDECAR_URL": f"http://127.0.0.1:{engine_port}", "POTLUCK_PYTHON_PORT": str(engine_port),
           "NO_COLOR": "1", "POTLUCK_SIDECAR_TOKEN": "", "POTLUCK_LOCAL_API_SOCKET": str(tmp_path / "absent.sock")}

    async def cli(*args, code=0):
        process = await asyncio.create_subprocess_exec("node", str(ROOT / "apps/cli/dist/index.js"), *args,
            cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == code, f"CLI {args[0]} failed: {stderr.decode()} {stdout.decode()}"
        result = json.loads(stdout)
        assert "private" not in stdout.decode()
        return result

    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        configured = await cli("setup", "--no-input", "--enable-gateway", "--json")
        assert configured["state"] == "configured"
        assert configured["scope"] == "local"
        first = await cli("login", "--no-wait", "--json")
        assert first["state"] == "pending"
        await cli("down", "--json")
        await cli("up", "--json")
        resumed = await cli("login", "--json")
        assert resumed["userCode"] == first["userCode"]
        await client.post("/auth/device/request-link", json={"user_code": first["userCode"], "email": "ssh-test@example.com"})
        proof = parse_qs(urlsplit(messages[0][1]).fragment)["token"][0]
        assert (await client.post("/auth/device/approve", json={"token": proof, "user_code": first["userCode"], "approved": True})).status_code == 200
        approved = await cli("login", "--wait", "--json")
        assert approved["state"] == "signed_in"
        assert approved["email"] == "ssh-test@example.com"
        stored = json.loads((tmp_path / "auth/member-session.json").read_text())
        bearer = stored["token"]
        assert bearer not in json.dumps(approved)
        await cli("down", "--json")
        await cli("up", "--json")
        assert (await cli("whoami", "--json"))["signedIn"] is True
        assert (await cli("logout", "--json"))["revoked"] is True
        assert (await client.get("/me", headers={"Authorization": "Bearer " + bearer})).status_code == 401
        assert (await cli("whoami", "--json", code=1))["signedIn"] is False
        assert json.loads((tmp_path / "auth/member-session.json").read_text())["token"] is None
        assert (await cli("gateway", "status", "--json"))["enabled"] is True
    finally:
        try:
            await cli("down", "--json")
        finally:
            server.should_exit = True
            await serving
            listener.close()
