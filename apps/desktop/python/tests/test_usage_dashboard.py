import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers.usage import router
from potluck_local.services import usage_dashboard as usage
from potluck_local.services.memory_db import _CREATE_USAGE_EVENTS

NOW = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)


def test_consistent_window_direction_rates_and_unknowns(tmp_path):
    async def run():
        path = tmp_path / 'usage.db'
        async with aiosqlite.connect(path) as db:
            await db.execute(_CREATE_USAGE_EVENTS)
            await db.executemany('INSERT INTO usage_events (ts,direction,scope,model_slug,input_tokens,output_tokens,ttft_ms,duration_ms,ok,error_class) VALUES (?,?,?,?,?,?,?,?,?,?)', [
                ((NOW-timedelta(hours=1)).isoformat(), 'consumed', 'local', 'coder', 40, 20, 100, 2000, 1, None),
                ((NOW-timedelta(hours=2)).isoformat(), 'consumed', 'my_machines', 'coder', 50, 80, 300, 3000, 1, None),
                ((NOW-timedelta(hours=3)).isoformat(), 'consumed', 'local', 'other', 0, 0, None, None, 1, None),
                ((NOW-timedelta(hours=4)).isoformat(), 'consumed', 'local', 'coder', 10, 999, 9999, 100, 0, 'never-expose-message'),
                ((NOW-timedelta(days=2)).isoformat(), 'consumed', 'local', 'old', 99, 9999, 9999, 9999, 1, None),
                (NOW.isoformat(), 'served', 'pool', 'contributor', 2, 7, None, None, 1, None),
                ((NOW+timedelta(seconds=1)).isoformat(), 'consumed', 'local', 'future', 99, 999, 999, 999, 1, None)])
            await db.commit()
        result = await usage.usage_snapshot(1, 'consumed', db_path=path, now=NOW)
        assert result['totals'] == dict(requests=4, inputTokens=100, outputTokens=1099, failures=1,
            avgTtftMs=200, avgDurationMs=2500, p95DurationMs=3000, endToEndTokensPerSecond=20, rateSamples=2)
        assert len(result['activity']) == 25
        assert sum(row['requests'] for row in result['activity']) == 4
        assert sum(row['requests'] for row in result['models']) == 4
        assert sum(row['requests'] for row in result['routes']) == 4
        assert len(result['recent']) == 4
        assert 'never-expose' not in str(result)
        assert result['models'][1]['endToEndTokensPerSecond'] is None
        served = await usage.usage_snapshot(1, 'served', db_path=path, now=NOW)
        assert served['totals']['requests'] == 1
        assert served['totals']['avgTtftMs'] is None
        assert served['totals']['endToEndTokensPerSecond'] is None
    asyncio.run(run())


def test_bounds_zero_filled_chart_and_nearest_rank(tmp_path):
    async def run():
        path = tmp_path / 'usage.db'
        async with aiosqlite.connect(path) as db:
            await db.execute(_CREATE_USAGE_EVENTS)
            await db.executemany('INSERT INTO usage_events (ts,direction,scope,model_slug,duration_ms) VALUES (?,?,?,?,?)',
                [(NOW.isoformat(), 'consumed', 'local', f'model-{i % 30}', i) for i in range(1, 201)])
            await db.commit()
        r = await usage.usage_snapshot(90, db_path=path, now=NOW)
        assert r['totals']['requests'] == 200 and r['totals']['p95DurationMs'] == 190
        assert len(r['recent']) == 100 and len(r['models']) == 20 and len(r['activity']) == 91
        assert r['recent'][0]['id'] == 200 and r['recent'][-1]['id'] == 101
        assert sum(row['requests'] for row in r['activity']) == 200
        empty = await usage.usage_snapshot(7, 'served', db_path=path, now=NOW)
        assert empty['totals']['requests'] == 0 and empty['totals']['p95DurationMs'] is None
        assert empty['recent'] == [] and len(empty['activity']) == 8
        for window, direction in [(0, 'consumed'), (3650, 'served'), (7, 'all')]:
            with pytest.raises(ValueError):
                await usage.usage_snapshot(window, direction, db_path=path)
    asyncio.run(run())


def test_api_parses_query_params_without_bypassing_bounds(monkeypatch):
    handler = AsyncMock(return_value={'schemaVersion': 1})
    monkeypatch.setattr(usage, 'dashboard', handler)
    app = FastAPI(); app.include_router(router, prefix='/usage')
    client = TestClient(app)
    assert client.get('/usage/dashboard?window=1&direction=served&household=true').status_code == 200
    handler.assert_awaited_once_with(1, 'served', True)
    for query in ('window=3', 'window=3650', 'window=hello', 'direction=other'):
        assert client.get('/usage/dashboard?' + query).status_code == 422
    assert handler.await_count == 1


def test_split_is_allowlisted_and_household_is_explicit(monkeypatch):
    from potluck_local.services import performance_status, split_manager
    snapshot = AsyncMock(return_value={})
    monkeypatch.setattr(usage, 'usage_snapshot', snapshot)
    perf = AsyncMock(return_value=dict(schemaVersion=1, sampledAt='now', local={}, household=None, secret='never-expose'))
    monkeypatch.setattr(performance_status, 'performance_status', perf)
    class Manager:
        def status(self):
            return dict(state='ready', workerEnabled=False, workerBusy=False, error='never-expose', session={
                'id': 'never-expose', 'model': {'id': 'coder', 'path': 'never-expose'},
                'workers': [{'name': 'never-expose', 'mesh_ip': 'never-expose'}]})
    monkeypatch.setattr(split_manager, 'get_split_manager', Manager)
    result = asyncio.run(usage.dashboard(7, 'consumed', False))
    perf.assert_awaited_once_with(include_household=False)
    assert result['split'] == dict(state='ready', workerEnabled=False, workerBusy=False, workers=1, model='coder')
    assert 'never-expose' not in str(result)
