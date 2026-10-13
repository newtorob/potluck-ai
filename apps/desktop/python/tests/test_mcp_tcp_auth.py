"""Tests for the MCP TCP/SSE transport auth gate.

Covers:
1. _TokenAuthMiddleware rejects requests with no/wrong token, accepts the
   right one - the actual enforcement mechanism.
2. main(["--tcp", ...]) refuses to start (SystemExit) when neither
   POTLUCK_MCP_TOKEN nor POTLUCK_SIDECAR_TOKEN is set - the fail-safe.
3. stdio transport is completely unaffected (no token required, no auth
   middleware involved) - the back-compat requirement.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import potluck_local.mcp.server as mcp_server


def _inner_app() -> Starlette:
    async def ok(request):  # noqa: ANN001, ARG001
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/probe", ok)])


# ---------------------------------------------------------------------------
# _TokenAuthMiddleware
# ---------------------------------------------------------------------------


def test_tcp_request_without_token_is_rejected() -> None:
    app = mcp_server._TokenAuthMiddleware(_inner_app(), token="s3cret")  # noqa: SLF001
    client = TestClient(app)
    r = client.get("/probe")
    assert r.status_code == 401


def test_tcp_request_with_wrong_token_is_rejected() -> None:
    app = mcp_server._TokenAuthMiddleware(_inner_app(), token="s3cret")  # noqa: SLF001
    client = TestClient(app)
    r = client.get("/probe", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_tcp_request_with_correct_token_is_accepted() -> None:
    app = mcp_server._TokenAuthMiddleware(_inner_app(), token="s3cret")  # noqa: SLF001
    client = TestClient(app)
    r = client.get("/probe", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_tcp_malformed_auth_header_is_rejected() -> None:
    app = mcp_server._TokenAuthMiddleware(_inner_app(), token="s3cret")  # noqa: SLF001
    client = TestClient(app)
    # No "Bearer " prefix
    r = client.get("/probe", headers={"Authorization": "s3cret"})
    assert r.status_code == 401


def test_real_sse_app_rejects_unauthenticated_get() -> None:
    """End-to-end-ish: wrap the actual FastMCP-generated Starlette app."""
    mcp = mcp_server._build_mcp(host="127.0.0.1", port=0)
    protected = mcp_server._TokenAuthMiddleware(mcp.sse_app(), token="s3cret")
    client = TestClient(protected)
    r = client.get("/sse")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# main() fail-safe: refuse to start --tcp without a token
# ---------------------------------------------------------------------------


def test_main_refuses_tcp_without_any_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("POTLUCK_MCP_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN", raising=False)
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--tcp", "18322"])
    assert exc_info.value.code == 1


def test_main_accepts_sidecar_token_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """POTLUCK_SIDECAR_TOKEN alone (no POTLUCK_MCP_TOKEN) is a valid credential.

    We don't want to actually bind a socket in a unit test, so we patch
    _serve_sse_authenticated (the function main() hands the token to) with a
    stand-in that records the token and raises a sentinel - proving main()
    got past the fail-safe guard and resolved the fallback env var correctly,
    without spinning up uvicorn.
    """
    monkeypatch.delenv("POTLUCK_MCP_TOKEN", raising=False)
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "fallback-token")
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    captured: dict[str, str] = {}

    class _ReachedServe(Exception):
        pass

    async def _fake_serve(mcp, token: str) -> None:  # noqa: ANN001
        captured["token"] = token
        raise _ReachedServe

    monkeypatch.setattr(mcp_server, "_serve_sse_authenticated", _fake_serve)

    with pytest.raises(_ReachedServe):
        mcp_server.main(["--tcp", "18323"])

    assert captured["token"] == "fallback-token"


# ---------------------------------------------------------------------------
# stdio transport is unaffected
# ---------------------------------------------------------------------------


def test_resolve_transport_stdio_default() -> None:
    assert mcp_server._resolve_transport() == "stdio"  # noqa: SLF001 -- argv-based, no args passed


def test_stdio_build_does_not_require_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building the stdio-transport FastMCP instance never touches token env vars."""
    monkeypatch.delenv("POTLUCK_MCP_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN", raising=False)
    mcp = mcp_server._build_mcp()  # noqa: SLF001
    assert mcp is not None
