"""Tests for GET /health.

Self-contained: no database, no env file, no external services required.
The health endpoint reads only from get_settings() (which has safe defaults)
and __version__; neither requires a running DB or network.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Force a deterministic environment before app import so that get_settings()
# (which is lru_cache'd) sees a clean state in test runs.
os.environ.setdefault("ENV", "development")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused:unused@localhost/test")
os.environ.setdefault("MAGIC_LINK_SECRET", "test-secret-not-real-64-chars-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


@pytest.fixture
def app():
    """Return the FastAPI app without triggering the lifespan (no DB needed)."""
    from potluck_api.app import create_app  # late import avoids module-level side effects

    return create_app()


@pytest.mark.asyncio
async def test_health_returns_200(app) -> None:
    """GET /health must return HTTP 200."""
    # ASGITransport runs the app in-process without a network socket.
    # We pass app directly so that the lifespan is NOT triggered — the health
    # endpoint has no lifespan dependencies.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_returns_status_ok(app) -> None:
    """GET /health body must contain ``status: ok``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_returns_version(app) -> None:
    """GET /health body must contain a non-empty ``version`` field."""
    from potluck_api import __version__

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    data = response.json()
    assert data["version"] == __version__
    assert len(data["version"]) > 0


@pytest.mark.asyncio
async def test_health_returns_env(app) -> None:
    """GET /health body must contain the ``env`` field."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    data = response.json()
    assert "env" in data
    assert data["env"] in {"development", "staging", "production"}
