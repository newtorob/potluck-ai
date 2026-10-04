"""Cross-service probes without either service's database-mutating test harness.

PYTHONPATH=services/api:services/coordinator services/api/.venv/bin/python \
    -m pytest scripts/tests/test_service_readiness.py -q
"""
from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture(params=["coordinator", "potluck_api"])
def service(request):
    return (
        importlib.import_module(request.param + ".routers.health"),
        importlib.import_module(request.param + ".db.database"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, ConnectionError("postgres://private:secret@host/db"), TimeoutError()])
async def test_readiness_reports_dependency_state_without_leaking_errors(service, monkeypatch, failure):
    health, _ = service
    probe = AsyncMock(side_effect=failure)
    monkeypatch.setattr(health, "check_database", probe)
    app = FastAPI()
    app.include_router(health.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
        assert response.status_code == (503 if failure else 200)
        assert response.json() == {"status": "unavailable" if failure else "ok", "database": "unavailable" if failure else "ok"}
        probe.assert_awaited_once()
        # A failed dependency must not make the process-liveness probe fail.
        assert (await client.get("/health")).status_code == 200
        probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninitialized_database_is_not_ready(service, monkeypatch):
    _, database = service
    monkeypatch.setattr(database, "_engine", None)
    with pytest.raises(RuntimeError, match="not initialised"):
        await database.check_database()


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", [None, "connect", "query"])
async def test_probe_deadline_covers_pool_checkout_and_query(service, monkeypatch, stall):
    _, database = service
    cleaned_up = False
    execute = AsyncMock()
    if stall == "query":
        async def wait_forever(*_):
            await asyncio.Event().wait()

        execute.side_effect = wait_forever

    @asynccontextmanager
    async def connect():
        nonlocal cleaned_up
        try:
            if stall == "connect":
                await asyncio.Event().wait()
            yield SimpleNamespace(execute=execute)
        finally:
            cleaned_up = True

    monkeypatch.setattr(database, "_engine", SimpleNamespace(connect=connect))
    if stall:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(database.check_database(timeout=0.01), timeout=1)
    else:
        await database.check_database()
        assert str(execute.call_args.args[0]) == "SELECT 1"
    assert cleaned_up
