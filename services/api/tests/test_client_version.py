"""Tests for GET /client/version and the X-Potluck-Client-Version header.

Self-contained: the route reads only get_settings() (safe defaults), no
database or external service required. Follows the same no-DB pattern as
test_health.py.
"""
from __future__ import annotations

import os

import pytest
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
async def test_client_version_returns_200(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/client/version")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_client_version_returns_default_fields(app) -> None:
    """Defaults must never falsely block today's build: latest == minimum == 0.1.0."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/client/version")

    data = response.json()
    assert data["latest"] == "0.1.0"
    assert data["minimum"] == "0.1.0"
    assert data["notesUrl"] == "https://trypotluck.ai"


@pytest.mark.asyncio
async def test_client_version_respects_env_overrides(monkeypatch) -> None:
    """CLIENT_LATEST_VERSION / CLIENT_MINIMUM_VERSION / CLIENT_NOTES_URL override defaults."""
    from potluck_api.config import get_settings

    monkeypatch.setenv("CLIENT_LATEST_VERSION", "0.3.0")
    monkeypatch.setenv("CLIENT_MINIMUM_VERSION", "0.2.0")
    monkeypatch.setenv("CLIENT_NOTES_URL", "https://trypotluck.ai/release-notes/0.3.0")
    get_settings.cache_clear()
    try:
        from potluck_api.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/client/version")

        data = response.json()
        assert data["latest"] == "0.3.0"
        assert data["minimum"] == "0.2.0"
        assert data["notesUrl"] == "https://trypotluck.ai/release-notes/0.3.0"
    finally:
        # Settings is process-cached; reset it so later tests see the defaults again.
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_request_without_client_version_header_is_never_rejected(app) -> None:
    """Absence of X-Potluck-Client-Version must never 400/401 — it's optional."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/client/version")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_request_with_client_version_header_is_never_rejected(app) -> None:
    """Presence of X-Potluck-Client-Version (any value, incl. garbage) must
    never 400/401 — enforcement is the app's job, not the server's, in this
    task."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/client/version",
            headers={"X-Potluck-Client-Version": "not-a-real-semver"},
        )
    assert response.status_code == 200

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/health",
            headers={"X-Potluck-Client-Version": "0.1.0"},
        )
    assert response.status_code == 200
