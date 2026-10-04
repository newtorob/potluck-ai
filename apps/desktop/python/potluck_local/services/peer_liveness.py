"""Detect an unreachable household worker without penalizing slow generation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import httpx

from .remote_stream import RemoteStreamError

QUIET_BEFORE_PROBE_S = 3.0
PROBE_TIMEOUT_S = 2.0
FAILED_PROBE_INTERVAL_S = 1.0
FAILED_PROBES_REQUIRED = 2


async def _reachable(health_url: str) -> bool:
    # Headers from any HTTP response prove reachability. A readiness error or
    # an older endpoint must not shorten the inference progress budget.
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
                async with client.stream("GET", health_url):
                    return True
    except (httpx.TransportError, TimeoutError):
        return False


async def watch_peer_stream(chunks: AsyncGenerator[dict, None], health_url: str) -> AsyncGenerator[dict, None]:
    """Race quiet streams against bounded health probes, never against tokens.

    No probes while useful output is arriving. Two consecutive transport
    failures end the attempt; any useful output resets that evidence. A live
    but stalled worker still hits the caller's unchanged progress deadline.
    Keep the pending read alive during probes so output can win the race.
    """
    loop = asyncio.get_running_loop()
    next_probe_at = loop.time() + QUIET_BEFORE_PROBE_S
    failures = 0
    pending: asyncio.Task | None = None
    probe: asyncio.Task | None = None
    iterator = aiter(chunks)
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            if probe is None:
                await asyncio.wait({pending}, timeout=max(0, next_probe_at - loop.time()))
                if not pending.done():
                    probe = asyncio.create_task(_reachable(health_url))
            if probe is not None:
                await asyncio.wait({pending, probe}, return_when=asyncio.FIRST_COMPLETED)
            # Prefer observed output/completion to a simultaneous failed probe.
            if pending.done():
                try:
                    chunk = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                choice = chunk["choices"][0]
                useful = any(value for key, value in choice["delta"].items() if key != "role")
                if useful or choice["finish_reason"] is not None:
                    failures = 0
                    next_probe_at = loop.time() + QUIET_BEFORE_PROBE_S
                    if probe is not None:
                        probe.cancel()
                        await asyncio.gather(probe, return_exceptions=True)
                        probe = None
                yield chunk
            elif probe is not None:
                alive = probe.result()
                probe = None
                failures = 0 if alive else failures + 1
                if failures >= FAILED_PROBES_REQUIRED:
                    raise RemoteStreamError(
                        "The household computer became unreachable during inference.",
                        "peer_unreachable",
                    )
                next_probe_at = loop.time() + (
                    QUIET_BEFORE_PROBE_S if alive else FAILED_PROBE_INTERVAL_S
                )
    finally:
        tasks = [task for task in (pending, probe) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await iterator.aclose()
