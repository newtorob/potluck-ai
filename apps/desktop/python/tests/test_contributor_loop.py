"""The shared contributor loop against a fake coordinator (M2.5).

These tests drive the REAL loop code in contributor/client.py: the
handshake, heartbeats, job dispatch, the in-flight cap, reject handling and
the stop semantics. The coordinator is a small in-test websockets server
that speaks the wire protocol from schemas.py.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import uuid
from dataclasses import dataclass, field

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potluck_local.contributor import client as contributor
from potluck_local.contributor.config import Settings
from potluck_local.contributor.schemas import CapabilityEntry, JobAssignedMessage
from potluck_local.contributor.telemetry import Capacity


def run_async(fn):
    """The sidecar suite has no pytest-asyncio; run each scenario on its own
    loop like test_contributor_stream_cancellation does."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@dataclass
class FakeCoordinator:
    """Accepts one peer at a time, records everything it sends."""

    reject_reason: str | None = None
    heartbeat_interval_s: int = 1
    attestations: list[dict] = field(default_factory=list)
    heartbeats: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    connections: int = 0
    headers: list[dict] = field(default_factory=list)
    _conn: object = None
    _connected: asyncio.Event = field(default_factory=asyncio.Event)
    _server: object = None
    url: str = ""

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/peers/connect"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, ws):
        self.connections += 1
        self.headers.append(dict(ws.request_headers))
        nonce = base64.urlsafe_b64encode(b"n" * 32).rstrip(b"=").decode()
        await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
        attest = json.loads(await ws.recv())
        self.attestations.append(attest)
        if self.reject_reason:
            await ws.send(json.dumps({"type": "reject", "reason": self.reject_reason}))
            await ws.close(code=1008)
            return
        await ws.send(json.dumps({
            "type": "accept",
            "peer_id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "server_time": "2026-09-10T00:00:00Z",
            "heartbeat_interval_s": self.heartbeat_interval_s,
        }))
        self._conn = ws
        self._connected.set()
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                if msg.get("type") == "heartbeat":
                    self.heartbeats.append(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._connected.clear()
            self._conn = None

    async def wait_connected(self, timeout=5.0):
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def assign(self, job_id=None, slug="fake-slug", prompt="hi"):
        job_id = job_id or uuid.uuid4()
        await self._conn.send(json.dumps({
            "type": "job_assigned", "job_id": str(job_id), "model_slug": slug,
            "prompt": prompt, "sampling": {"max_tokens": 8},
        }))
        return job_id

    async def wait_for_frame(self, predicate, timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for f in self.frames:
                if predicate(f):
                    return f
            await asyncio.sleep(0.02)
        raise AssertionError("frame not seen")


def _settings(url: str, **over) -> Settings:
    base = dict(coordinator_url=url, binary_hash="test-hash", member_id=uuid.UUID(int=1))
    base.update(over)
    return Settings(**base)


def _hooks(**over) -> contributor.LoopHooks:
    events: list[tuple[str, dict]] = []

    async def job_handler(ws, job: JobAssignedMessage):
        await ws.send(json.dumps({"type": "job_token", "job_id": str(job.job_id),
                                  "token_index": 0, "token_text": "ok"}))
        await ws.send(json.dumps({"type": "job_complete", "job_id": str(job.job_id),
                                  "output_tokens": 1, "duration_ms": 1,
                                  "finish_reason": "stop"}))

    async def capacity():
        return Capacity(gpu_utilization=0.25, vram_available_gb=7.5)

    async def capabilities():
        return [CapabilityEntry(model_slug="fake-slug", quantization="q4",
                                tokens_per_second=41.5, prompt_tokens_per_second=380.0)]

    hooks = contributor.LoopHooks(
        job_handler=job_handler,
        capacity_probe=capacity,
        capabilities_provider=capabilities,
        hardware_summary_provider=lambda: {"system": "TestOS", "gpu_name": "FakeGPU",
                                           "gpu_vram_gb": 11.0, "recommended_tier": "medium"},
        headers_provider=lambda: [("Authorization", "Bearer sess-123")],
        max_concurrent_jobs=1,
        on_state=lambda event, **f: events.append((event, f)),
    )
    for k, v in over.items():
        setattr(hooks, k, v)
    hooks.events = events  # type: ignore[attr-defined]
    return hooks


@run_async
async def test_attestation_carries_hardware_capabilities_and_headers():
    async with FakeCoordinator() as coord:
        hooks = _hooks()
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord.wait_connected()
        stop.set()
        await asyncio.wait_for(task, 5)

    attest = coord.attestations[0]
    assert attest["hardware_summary"]["gpu_name"] == "FakeGPU"
    assert attest["hardware_summary"]["recommended_tier"] == "medium"
    cap = attest["capabilities"][0]
    assert cap["tokens_per_second"] == 41.5
    assert cap["prompt_tokens_per_second"] == 380.0
    assert attest["client_version"] == contributor.CLIENT_VERSION
    assert coord.headers[0].get("authorization") == "Bearer sess-123"
    kinds = [e for e, _ in hooks.events]
    assert kinds[:2] == ["connecting", "connected"]
    assert kinds[-1] == "stopped"


@run_async
async def test_heartbeat_carries_live_capacity_and_cap():
    async with FakeCoordinator(heartbeat_interval_s=1) as coord:
        hooks = _hooks(max_concurrent_jobs=2)
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord.wait_connected()
        hb = await coord.wait_for_frame(lambda f: f.get("type") == "heartbeat", timeout=4)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert hb["current_jobs"] == 0
    assert hb["gpu_utilization"] == 0.25
    assert hb["vram_available_gb"] == 7.5
    assert hb["max_concurrent_jobs"] == 2


@run_async
async def test_job_streams_and_in_flight_is_visible_then_clears():
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(ws, job):
        started.set()
        await release.wait()
        await ws.send(json.dumps({"type": "job_complete", "job_id": str(job.job_id),
                                  "output_tokens": 0, "duration_ms": 1,
                                  "finish_reason": "stop"}))

    async with FakeCoordinator(heartbeat_interval_s=1) as coord:
        hooks = _hooks(job_handler=slow_handler)
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord.wait_connected()
        job_id = await coord.assign()
        await asyncio.wait_for(started.wait(), 5)
        busy_hb = await coord.wait_for_frame(
            lambda f: f.get("type") == "heartbeat" and f["current_jobs"] == 1, timeout=4)
        assert busy_hb["current_jobs"] == 1
        release.set()
        await coord.wait_for_frame(lambda f: f.get("type") == "job_complete")
        idle_hb_count_before = len(coord.heartbeats)
        await coord.wait_for_frame(
            lambda f: f.get("type") == "heartbeat" and f["current_jobs"] == 0
            and coord.heartbeats.index(f) >= idle_hb_count_before - 1, timeout=4)
        stop.set()
        await asyncio.wait_for(task, 5)
    kinds = [e for e, _ in hooks.events]
    assert "job_started" in kinds and "job_finished" in kinds
    finished = [f for e, f in hooks.events if e == "job_finished"][0]
    assert finished == {"job_id": str(job_id), "ok": True}


@run_async
async def test_second_job_is_declined_at_cap_one():
    release = asyncio.Event()

    async def slow_handler(ws, job):
        await release.wait()
        await ws.send(json.dumps({"type": "job_complete", "job_id": str(job.job_id),
                                  "output_tokens": 0, "duration_ms": 1,
                                  "finish_reason": "stop"}))

    async with FakeCoordinator() as coord:
        hooks = _hooks(job_handler=slow_handler, max_concurrent_jobs=1)
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord.wait_connected()
        first = await coord.assign()
        await asyncio.sleep(0.1)
        second = await coord.assign()
        err = await coord.wait_for_frame(lambda f: f.get("type") == "job_error")
        assert err["job_id"] == str(second)
        assert err["error_category"] == "declined"
        assert "capacity" in err["error_message"]
        release.set()
        done = await coord.wait_for_frame(lambda f: f.get("type") == "job_complete")
        assert done["job_id"] == str(first)
        stop.set()
        await asyncio.wait_for(task, 5)


@run_async
async def test_reject_surfaces_reason_and_backs_off(monkeypatch):
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 0.05)
    async with FakeCoordinator(reject_reason="peer_public_key is not registered for any member") as coord:
        hooks = _hooks()
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        deadline = asyncio.get_event_loop().time() + 3
        while coord.connections < 2 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 5)
    assert coord.connections >= 2, "should retry after a reject"
    rejected = [f for e, f in hooks.events if e == "rejected"]
    assert rejected and "not registered" in rejected[0]["reason"]


@run_async
async def test_stop_closes_socket_and_does_not_reconnect():
    async with FakeCoordinator() as coord:
        hooks = _hooks()
        stop = asyncio.Event()
        task = asyncio.create_task(contributor.run_contributor_loop(
            _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
        await coord.wait_connected()
        stop.set()
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.3)
    assert coord.connections == 1
    assert coord._conn is None


@run_async
async def test_stop_during_backoff_returns_promptly(monkeypatch):
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 30.0)
    hooks = _hooks()
    stop = asyncio.Event()
    # Nothing listens on this port: connect fails, loop enters a 30 s backoff.
    task = asyncio.create_task(contributor.run_contributor_loop(
        _settings("ws://127.0.0.1:1/peers/connect"), Ed25519PrivateKey.generate(), hooks, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 2)
    kinds = [e for e, _ in hooks.events]
    assert "disconnected" in kinds and kinds[-1] == "stopped"


def test_standalone_hooks_use_settings_cap_and_token():
    s = Settings(coordinator_url="ws://x/peers/connect", binary_hash="h",
                 coordinator_token="shared-secret", max_concurrent_jobs=3)
    hooks = contributor.standalone_hooks(s)
    assert hooks.max_concurrent_jobs == 3
    assert hooks.headers_provider() == [("X-Potluck-Coordinator-Token", "shared-secret")]
    s2 = Settings(coordinator_url="ws://x/peers/connect", binary_hash="h")
    assert contributor.standalone_hooks(s2).headers_provider() is None
