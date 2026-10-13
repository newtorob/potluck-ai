"""Tests for GET /models/catalog - the cloud feed the desktop sidecar polls.

Self-contained: the route reads a bundled static JSON file, no database or
external service required. Follows the same no-DB pattern as test_health.py.
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
async def test_catalog_returns_200(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/models/catalog")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_catalog_returns_14_models(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/models/catalog")

    data = response.json()
    assert len(data["models"]) == 14


@pytest.mark.asyncio
async def test_catalog_has_wrapper_keys(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/models/catalog")

    data = response.json()
    assert set(data.keys()) == {"schema_version", "updated_at", "models"}
    assert isinstance(data["schema_version"], int)
    assert isinstance(data["updated_at"], str) and data["updated_at"]


@pytest.mark.asyncio
async def test_catalog_entries_carry_required_fields(app) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/models/catalog")

    data = response.json()
    for entry in data["models"]:
        assert entry.get("slug")
        assert entry.get("download_url")
        assert entry.get("sha256")


@pytest.mark.asyncio
async def test_stub_routes_still_intact(app) -> None:
    """The pre-existing stub routes must keep working alongside /catalog."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get("/models/")
        manifest_resp = await client.get("/models/nonexistent/manifest")

    assert list_resp.status_code == 200
    assert list_resp.json() == []
    assert manifest_resp.status_code == 404
