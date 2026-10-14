import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from potluck_local.services import performance_status as perf, usage_store


def test_household_metrics_are_aggregated_and_incomplete_probes_are_explicit(monkeypatch):
    peers = [{'mesh_ip': 'never-expose-1'}, {'mesh_ip': 'never-expose-2'}, {'mesh_ip': 'never-expose-3'}]
    monkeypatch.setattr(perf.gateway_routing, 'household_peers', AsyncMock(return_value=peers))
    async def caps(client, peer):
        if peer == peers[2]: return None
        return {'hostname': 'never-expose-name', 'node_id': 'never-expose-id', 'loaded_model': 'coder',
                'available_for_inference': True, 'inference_load': {'busy': peer == peers[0]},
                'loaded_model_detail': {'tokens_per_sec': 10 if peer == peers[0] else None}}
    monkeypatch.setattr(perf.gateway_routing, '_peer_caps', caps)
    result = asyncio.run(perf.household_status())
    assert result == {'rosterCount': 3, 'checked': 2, 'available': 2, 'busy': 1, 'partial': True,
                      'models': [{'model': 'coder', 'machines': 2, 'benchmarkedMachines': 1, 'meanBenchmarkTokensPerSecond': 10}]}
    assert 'never-expose' not in str(result)


def test_local_metrics_do_not_generate_or_probe_household_and_keep_unknowns_null(monkeypatch):
    runtime = SimpleNamespace(current_model_id=lambda: 'coder', benchmarks=lambda: {},
        loaded_n_ctx=lambda: 4096, continuous_batch_active=lambda: False,
        busy=lambda: True)
    monkeypatch.setattr(perf, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(perf.psutil, 'virtual_memory', lambda: SimpleNamespace(available=2 * 1024 ** 3))
    monkeypatch.setattr(perf.usage_store, 'performance_summary', AsyncMock(side_effect=RuntimeError('db unavailable')))
    probe = AsyncMock(); monkeypatch.setattr(perf, 'household_status', probe)
    result = asyncio.run(perf.performance_status(False))
    assert result['local']['busy'] is True and result['local']['memoryAvailableGiB'] == 2
    assert result['local']['benchmarkTokensPerSecond'] is None and result['usage'] is None
    probe.assert_not_called()


def test_usage_metrics_include_only_recent_consumed_events_and_keep_missing_samples_null(tmp_path):
    async def run():
        path = tmp_path / 'usage.db'
        async with aiosqlite.connect(path) as db:
            await db.execute('CREATE TABLE usage_events (direction TEXT, ts TEXT, output_tokens INTEGER, ttft_ms INTEGER, duration_ms INTEGER, ok INTEGER)')
            await db.commit()
        empty = await usage_store.performance_summary(path)
        assert empty['requests'] == 0 and empty['avgTtftMs'] is None and empty['successRate'] is None
        now = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        async with aiosqlite.connect(path) as db:
            await db.executemany('INSERT INTO usage_events VALUES (?,?,?,?,?,?)', [
                ('consumed', now, 10, 100, 1000, 1), ('consumed', now, 0, None, 2000, 0),
                ('served', now, 999, 999, 9999, 1), ('consumed', old, 999, 999, 9999, 1)])
            await db.commit()
        result = await usage_store.performance_summary(path)
        assert result == dict(windowHours=24, requests=2, reportedOutputTokens=10, avgTtftMs=100,
                              avgDurationMs=1500, successRate=.5)
    asyncio.run(run())
