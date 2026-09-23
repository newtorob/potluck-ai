"""Read-only operational metrics. No text, addresses, names, keys, or peer IDs."""
import asyncio
import math
from datetime import datetime, timezone

import httpx
import psutil

from potluck_local.services import gateway_routing, usage_store
from potluck_local.services.runtime import get_runtime


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


async def household_status() -> dict:
    peers = await gateway_routing.household_peers()
    semaphore = asyncio.Semaphore(8)
    results = []
    async with httpx.AsyncClient(timeout=2) as client:
        async def probe(peer):
            async with semaphore:
                caps = await gateway_routing._peer_caps(client, peer)
                results.append(caps)
        tasks = [asyncio.create_task(probe(peer)) for peer in peers[:64]]
        try:
            if tasks:
                await asyncio.wait(tasks, timeout=2.1)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    models = {}
    checked = available = busy = 0
    for caps in results:
        if not isinstance(caps, dict): continue
        checked += 1
        if caps.get('available_for_inference') is not True: continue
        available += 1
        busy += (caps.get('inference_load') or {}).get('busy') is True
        model = caps.get('loaded_model')
        if not isinstance(model, str) or not model: continue
        row = models.setdefault(model, {'model': model, 'machines': 0, 'rates': []})
        row['machines'] += 1
        rate = number((caps.get('loaded_model_detail') or {}).get('tokens_per_sec'))
        if rate is not None: row['rates'].append(rate)
    return {'rosterCount': len(peers), 'checked': checked, 'available': available, 'busy': busy,
            'partial': checked < len(peers), 'models': [
                {'model': row['model'], 'machines': row['machines'], 'benchmarkedMachines': len(row['rates']),
                 'meanBenchmarkTokensPerSecond': sum(row['rates']) / len(row['rates']) if row['rates'] else None}
                for row in sorted(models.values(), key=lambda row: row['model'])]}


async def performance_status(include_household: bool = False) -> dict:
    runtime = get_runtime()
    model = runtime.current_model_id()
    benchmark = runtime.benchmarks().get(model) or {}
    memory = psutil.virtual_memory()
    try: usage = await usage_store.performance_summary()
    except Exception: usage = None
    return {'schemaVersion': 1, 'sampledAt': datetime.now(timezone.utc).isoformat(),
            'local': {'model': model, 'busy': runtime._inference_lock.locked(),
                      'contextTokens': runtime.loaded_n_ctx(), 'continuousBatch': runtime.continuous_batch_active(),
                      'memoryAvailableGiB': round(memory.available / 1024 ** 3, 2),
                      'benchmarkTokensPerSecond': number(benchmark.get('tokens_per_sec')),
                      'benchmarkTtftMs': number(benchmark.get('ttft_ms'))},
            'usage': usage, 'household': await household_status() if include_household else None}
