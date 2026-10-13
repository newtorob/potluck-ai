"""How long the contributor loop waits before it connects again.

Two rules, both small:

  * A reconnect the engine itself asks for (what it can serve changed, so it must
    attest again) comes straight back and leaves the backoff alone.
  * A session that was accepted and lasted HEALTHY_SESSION_S starts the backoff
    over when it ends, instead of continuing from where earlier failures left it.

Failures keep doubling up to the maximum.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import time
import uuid
from dataclasses import dataclass, field

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potluck_local.contributor import client as contributor
from potluck_local.contributor.config import Settings
from potluck_local.contributor.schemas import CapabilityEntry
from potluck_local.contributor.telemetry import Capacity


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@dataclass
class Coordinator:
    """Accepts every connection and records when each one began and ended. A
    connection can be told to hang up `hold_s` seconds after its accept (the next
    entry of `holds`, or the default) or to be rejected."""

    holds: list[float] = field(default_factory=list)
    default_hold_s: float | None = None  # None: keep the connection until the peer leaves
    reject_first: int = 0
    began: list[float] = field(default_factory=list)  # when each connection reached the coordinator
    ended: list[float] = field(default_factory=list)  # when each one closed
    attestations: list[dict] = field(default_factory=list)
    url: str = ""
    _server: object = None

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/peers/connect"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        index = len(self.began)
        self.began.append(time.monotonic())
        try:
            nonce = base64.urlsafe_b64encode(b"n" * 32).rstrip(b"=").decode()
            await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
            self.attestations.append(json.loads(await ws.recv()))
            if index < self.reject_first:
                await ws.send(json.dumps({"type": "reject", "reason": "no"}))
                await ws.close(code=1008)
                return
            await ws.send(json.dumps({
                "type": "accept", "peer_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
                "server_time": "2026-10-09T00:00:00Z", "heartbeat_interval_s": 30}))
            hold = self.holds[index - self.reject_first] if index - self.reject_first < len(self.holds) else self.default_hold_s
            if hold is not None:
                await asyncio.sleep(hold)
                await ws.close(code=1011)  # not a clean 1000: a failure from the engine's side
                return
            async for _ in ws:
                pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.ended.append(time.monotonic())

    def gaps(self) -> list[float]:
        """Seconds from each connection ending to the next one beginning."""
        return [self.began[i + 1] - self.ended[i] for i in range(min(len(self.ended), len(self.began) - 1))]


def settings(url: str) -> Settings:
    return Settings(coordinator_url=url, binary_hash="test-hash", member_id=uuid.UUID(int=1))


def hooks(loaded: list[str], events: list | None = None) -> contributor.LoopHooks:
    async def job_handler(ws, job):
        return None

    async def capacity():
        return Capacity()

    async def capabilities():
        return [CapabilityEntry(model_slug=slug, quantization="q4") for slug in loaded]

    return contributor.LoopHooks(
        job_handler=job_handler, capacity_probe=capacity, capabilities_provider=capabilities,
        hardware_summary_provider=lambda: {}, headers_provider=lambda: None, max_concurrent_jobs=1,
        on_state=(lambda event, **f: events.append(event)) if events is not None else None)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(contributor, "CAPABILITY_CHECK_INTERVAL_S", 0.03)
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 0.3)
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_MAX_S", 2.4)
    monkeypatch.setattr(contributor, "REATTEST_PAUSE_S", 0.02)
    monkeypatch.setattr(contributor, "HEALTHY_SESSION_S", 0.5)


async def start(coord: Coordinator, loaded: list[str], events: list | None = None):
    stop = asyncio.Event()
    task = asyncio.create_task(contributor.run_contributor_loop(
        settings(coord.url), Ed25519PrivateKey.generate(), hooks(loaded, events), stop))
    return task, stop


async def until(predicate, timeout=6.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def finish(task, stop):
    stop.set()
    await asyncio.wait_for(task, 5)


# ---- a reconnect the engine asked for ------------------------------------------------------------------


@run_async
async def test_several_reattestations_in_a_row_each_come_straight_back_and_the_backoff_does_not_grow():
    loaded = ["m0"]
    events: list[str] = []
    async with Coordinator() as coord:
        task, stop = await start(coord, loaded, events)
        for n in range(1, 6):
            await until(lambda n=n: len(coord.attestations) >= n)
            await asyncio.sleep(0.1)
            loaded[:] = [f"m{n}"]  # what the engine can serve changes: it must attest again
        await until(lambda: len(coord.attestations) >= 6)
        await asyncio.sleep(0.2)
        await finish(task, stop)
    gaps = coord.gaps()
    assert len(coord.attestations) == 6 and len(gaps) >= 5
    # A backoff that doubled each time would wait 0.3, 0.6, 1.2, 2.4 s here.
    assert max(gaps[:5]) < 0.25, gaps
    assert [c["model_slug"] for c in coord.attestations[-1]["capabilities"]] == ["m5"]
    assert "disconnected" not in events, "a re-attestation is not reported as a failure"
    assert events.count("reattest") == 5


@run_async
async def test_a_reattestation_leaves_the_backoff_where_a_failure_had_put_it():
    loaded = ["a"]
    async with Coordinator(holds=[0.0, 0.0]) as coord:  # two failed sessions, then steady
        task, stop = await start(coord, loaded)
        await until(lambda: len(coord.attestations) >= 3)
        await asyncio.sleep(0.1)
        loaded[:] = ["b"]
        await until(lambda: len(coord.attestations) >= 4)
        await asyncio.sleep(0.1)
        await finish(task, stop)
    gaps = coord.gaps()
    assert gaps[0] >= 0.28 and gaps[1] >= 0.58, gaps  # failures: 0.3 then 0.6
    assert gaps[2] < 0.25, gaps  # the engine's own: prompt


@run_async
async def test_a_close_the_coordinator_made_is_still_a_failure_and_still_backs_off():
    async with Coordinator(holds=[0.0, 0.0, 0.0, 0.0]) as coord:
        task, stop = await start(coord, ["m"])
        await until(lambda: len(coord.began) >= 5, timeout=10)
        await finish(task, stop)
    gaps = coord.gaps()
    assert gaps[0] >= 0.28 and gaps[1] >= 0.58 and gaps[2] >= 1.18 and gaps[3] >= 2.38, gaps


@run_async
async def test_rejects_keep_backing_off():
    async with Coordinator(reject_first=4) as coord:
        task, stop = await start(coord, ["m"])
        await until(lambda: len(coord.began) >= 5, timeout=10)
        await finish(task, stop)
    gaps = coord.gaps()
    assert gaps[1] > gaps[0] and gaps[2] > gaps[1] and gaps[3] >= 1.18, gaps


# ---- a healthy session starts the backoff over -----------------------------------------------------------------


@run_async
async def test_a_session_that_lasted_long_enough_starts_the_backoff_over():
    # Three quick failures grow the backoff to 0.3, 0.6, 1.2 s. Then a session that
    # lasts 0.7 s (past HEALTHY_SESSION_S) ends the same way: the wait after it is
    # the initial 0.3 s, not the 2.4 s the doubling had reached. A quick failure
    # after that waits 0.6 s, the second step, as it should.
    async with Coordinator(holds=[0.0, 0.0, 0.0, 0.7, 0.0]) as coord:
        task, stop = await start(coord, ["m"])
        await until(lambda: len(coord.began) >= 6, timeout=15)
        await finish(task, stop)
    gaps = coord.gaps()
    assert gaps[0] >= 0.28 and gaps[1] >= 0.58 and gaps[2] >= 1.18, gaps
    assert 0.28 <= gaps[3] < 0.55, f"after a healthy session: {gaps}"
    assert gaps[4] >= 0.58, f"and the next plain failure is the second step again: {gaps}"


@run_async
async def test_a_session_shorter_than_that_does_not_start_it_over():
    async with Coordinator(holds=[0.0, 0.0, 0.2, 0.0]) as coord:  # 0.2 s < HEALTHY_SESSION_S
        task, stop = await start(coord, ["m"])
        await until(lambda: len(coord.began) >= 5, timeout=15)
        await finish(task, stop)
    gaps = coord.gaps()
    assert gaps[2] >= 1.18, f"0.3, 0.6, then 1.2 whatever the third session did: {gaps}"


@run_async
async def test_a_session_that_was_never_accepted_does_not_count_however_long_it_took():
    info = contributor._SessionInfo()
    assert not info.healthy()
    info.accepted_at = time.monotonic() - 3600
    assert info.healthy()
    info.accepted_at = time.monotonic()
    assert not info.healthy()


def test_the_numbers_are_the_agreed_ones():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(contributor.__file__).read_text())
    values = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              for t in n.targets if isinstance(t, ast.Name)}
    assert values["HEALTHY_SESSION_S"] == 30.0
    assert values["RECONNECT_BACKOFF_INITIAL_S"] == 1.0 and values["RECONNECT_BACKOFF_MAX_S"] == 60.0
    assert values["REATTEST_PAUSE_S"] == 0.25
