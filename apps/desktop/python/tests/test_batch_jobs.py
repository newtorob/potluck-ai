"""Batch acceptance: distribution, checkpoint recovery and failure boundaries."""
import asyncio
import json
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.routers.batches import Submit
from potluck_local.services import batch_jobs as batch
from potluck_local.services import gateway_routing as routing
from potluck_local.services.remote_stream import RemoteStreamError

PIN = {"id": "tiny", "sha256": "a" * 64}
WORKERS = [{"key": str(n), "name": f"Computer {n}"} for n in range(3)]


def submission(count=5, **changes):
    return Submit(**{"id": str(uuid.uuid4()), "model": PIN, "scope": "mine", "concurrency": 3,
                     "files": [{"name": f"document-{n}.txt", "text": f"Private input {n}"} for n in range(count)],
                     **changes}).model_dump(mode="json")


async def settled(manager, job_id):
    for _ in range(500):
        job = manager.store.get(job_id)
        if job["state"] not in {"queued", "running"} and manager.active_id != job_id:
            return job
        await asyncio.sleep(.002)
    raise AssertionError("Batch did not settle")


class Executor:
    def __init__(self):
        self.calls, self.busy, self.peak = [], set(), 0
        self.available = WORKERS
        self.block = None
        self.fail = set()

    async def workers(self, job):
        return self.available

    async def complete(self, job, item, worker):
        assert worker["key"] not in self.busy, "Two pieces assigned to one worker"
        assert job["model"] == PIN and job["scope"] in {"mine", "local"}
        self.calls.append((item["id"], worker["key"], item["input"]))
        self.busy.add(worker["key"])
        self.peak = max(self.peak, len(self.busy))
        try:
            if self.block:
                await self.block.wait()
            await asyncio.sleep(.005)
            if worker["key"] in self.fail:
                raise RemoteStreamError("Private backend detail", "peer_unreachable")
            return "Summary: " + item["input"]
        finally:
            self.busy.remove(worker["key"])


def test_parallel_distribution_complete_once_and_idempotent_submit(tmp_path):
    async def run():
        executor = Executor()
        manager = batch.BatchManager(tmp_path / "private", executor)
        body = submission()
        first = manager.submit(body)
        assert manager.submit(body)["id"] == first["id"]
        job = await settled(manager, first["id"])
        assert job["state"] == "completed"
        assert executor.peak == 3
        assert len(executor.calls) == 5
        assert {call[1] for call in executor.calls} == {"0", "1", "2"}
        assert all(len(i["attempts"]) == 1 and i["output"] for i in job["items"])
        assert "Private input" not in json.dumps(batch.summary(job))
        with pytest.raises(batch.BatchError, match="different submission"):
            manager.submit({**body, "scope": "local"})
        if os.name != "nt":
            assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
            assert (tmp_path / "private/jobs.sqlite3").stat().st_mode & 0o777 == 0o600
        await manager.close()
    asyncio.run(run())


def test_failed_worker_retries_elsewhere_without_repeating_success(tmp_path):
    async def run():
        executor = Executor(); executor.fail.add("0")
        manager = batch.BatchManager(tmp_path / "private", executor)
        job_id = manager.submit(submission())["id"]
        job = await settled(manager, job_id)
        assert job["state"] == "completed"
        assert sum(c[1] == "0" for c in executor.calls) == 1
        assert len(executor.calls) == 6
        retried = next(i for i in job["items"] if len(i["attempts"]) == 2)
        assert retried["attempts"][0]["error"] == "peer_unreachable"
        assert retried["attempts"][1]["worker"] != "Computer 0"
        assert "Private backend detail" not in json.dumps(job)
        await manager.close()
    asyncio.run(run())


def test_shutdown_and_reopen_preserve_snapshots_completed_work_and_attempt_budget(tmp_path):
    async def run():
        executor = Executor(); executor.available = WORKERS[:1]
        manager = batch.BatchManager(tmp_path / "private", executor)
        job_id = manager.submit(submission(3, concurrency=1))["id"]
        for _ in range(100):
            if any(i["state"] == "completed" for i in manager.store.get(job_id)["items"]):
                break
            await asyncio.sleep(.001)
        await manager.close()
        # Reopen without the original source files or any in-memory queue.
        other = Executor()
        restored = batch.BatchManager(tmp_path / "private", other)
        paused = restored.store.get(job_id)
        assert paused["state"] == "paused"
        done = {i["id"] for i in paused["items"] if i["state"] == "completed"}
        assert done and not other.calls
        with pytest.raises(batch.BatchError, match="original model"):
            restored.resume(job_id, {**PIN, "sha256": "b" * 64}, "mine")
        with pytest.raises(batch.BatchError, match="original model"):
            restored.resume(job_id, PIN, "local")
        restored.resume(job_id, PIN, "mine")
        assert (await settled(restored, job_id))["state"] == "completed"
        assert not done.intersection(c[0] for c in other.calls)
        assert all(c[2].startswith("Private input") for c in other.calls)
        await restored.close()
    asyncio.run(run())


def test_crash_recovery_releases_stale_lease_and_never_increases_attempt_limit(tmp_path):
    async def run():
        executor = Executor(); executor.available = []
        manager = batch.BatchManager(tmp_path / "private", executor)
        job_id = manager.submit(submission(1))["id"]
        await settled(manager, job_id)
        job = manager.store.get(job_id)
        job["state"] = "running"
        item = job["items"][0]
        item.update(state="running", lease="dead-process")
        item["attempts"] = [{"state": "running", "worker": "gone", "startedAt": 1}] * 3
        manager.store.save(job)
        manager.store.close()  # emulate abrupt process death without Manager.close
        restored = batch.BatchManager(tmp_path / "private", Executor())
        saved = restored.store.get(job_id)
        assert saved["state"] == "paused"
        assert saved["items"][0]["state"] == "failed"
        assert saved["items"][0]["lease"] is None
        with pytest.raises(batch.BatchError, match="attempts remaining"):
            restored.resume(job_id, PIN, "mine")
        await restored.close()
    asyncio.run(run())


def test_no_workers_pauses_and_cancel_drains_without_late_output(tmp_path):
    async def run():
        executor = Executor(); executor.available = []
        manager = batch.BatchManager(tmp_path / "private", executor)
        job_id = manager.submit(submission(2))["id"]
        assert (await settled(manager, job_id))["reason"] == "no_eligible_workers"
        assert not executor.calls
        executor.available = WORKERS; executor.block = asyncio.Event()
        manager.resume(job_id, PIN, "mine")
        for _ in range(100):
            if executor.busy: break
            await asyncio.sleep(.001)
        assert executor.busy
        await manager.cancel(job_id)
        cancelled = await settled(manager, job_id)
        assert cancelled["state"] == "cancelled" and not executor.busy
        assert all(i["output"] is None for i in cancelled["items"])
        executor.block = None
        manager.resume(job_id, PIN, "mine")
        assert (await settled(manager, job_id))["state"] == "completed"
        manager.delete(job_id)
        with pytest.raises(batch.BatchError): manager.store.get(job_id)
        await manager.close()
    asyncio.run(run())


def test_cancellation_during_probe_does_not_admit_work(tmp_path):
    async def run():
        gate, probing = asyncio.Event(), asyncio.Event()
        executor = Executor()
        async def workers(_):
            probing.set(); await gate.wait(); return WORKERS
        executor.workers = workers
        manager = batch.BatchManager(tmp_path / "private", executor)
        job_id = manager.submit(submission())["id"]
        await probing.wait()
        await manager.cancel(job_id)
        gate.set()
        assert (await settled(manager, job_id))["state"] == "cancelled"
        assert executor.calls == []
        await manager.close()
    asyncio.run(run())


def test_fifo_batches_do_not_share_worker_slots(tmp_path):
    async def run():
        executor = Executor()
        manager = batch.BatchManager(tmp_path / "private", executor)
        ids = [manager.submit(submission(4))["id"] for _ in range(2)]
        for job_id in ids: assert (await settled(manager, job_id))["state"] == "completed"
        assert executor.peak == 3 and len(executor.calls) == 8
        await manager.close()
    asyncio.run(run())


def test_scope_and_pin_filtering_never_calls_pool(monkeypatch):
    from potluck_local.services import runtime
    class Runtime:
        def assert_model_identity(self, *_): raise batch.ModelIdentityError()
        def is_loaded(self): return False
    monkeypatch.setattr(runtime, "get_runtime", lambda: Runtime())
    calls = []
    async def peers():
        calls.append("roster")
        return [{"mesh_ip": str(n), "node_name": f"n{n}"} for n in range(4)]
    async def caps(_, peer):
        n = int(peer["mesh_ip"])
        return {"available_for_inference": n != 2, "model_lock_version": 1,
                "batch_protocol_version": 0 if n == 3 else 1,
                "loaded_model": "tiny", "loaded_model_detail": {"sha256": "b" * 64 if n == 1 else PIN["sha256"]}}
    monkeypatch.setattr(routing, "household_peers", peers)
    monkeypatch.setattr(routing, "_peer_caps", caps)
    async def run():
        executor = batch.HouseholdExecutor()
        assert await executor.workers({"model": PIN, "scope": "local"}) == []
        assert calls == []
        workers = await executor.workers({"model": PIN, "scope": "mine"})
        assert [w["key"] for w in workers] == ["0"]
    asyncio.run(run())


@pytest.mark.parametrize("finish", ["length", None, "tool_calls"])
def test_incomplete_or_tool_output_cannot_be_committed(monkeypatch, finish):
    async def stream(*args, **kwargs):
        assert kwargs == {"sha256": PIN["sha256"], "retry": False, "batch": True}
        yield {"choices": [{"delta": {"content": "incomplete"}, "finish_reason": None}]}
        if finish: yield {"choices": [{"delta": {}, "finish_reason": finish}]}
    monkeypatch.setattr(routing, "stream_peer_openai", stream)
    async def run():
        with pytest.raises(RemoteStreamError):
            await batch.HouseholdExecutor().complete({"model": PIN, "maxTokens": 512, "systemPrompt": batch.SUMMARY_PROMPT}, {"input": "text"}, {"peer": {}})
    asyncio.run(run())


def test_truncated_real_http_stream_retries_whole_piece_and_discards_partial(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    calls = []
    def handler(request):
        body = json.loads(request.content)
        assert body["potluck_model_sha256"] == PIN["sha256"]
        assert len(body["messages"]) == 2 and "tools" not in body
        calls.append((request.url.host, body))
        content = 'data: {"choices":[{"delta":{"content":"' + ("discard me" if request.url.host == "100.64.0.2" else "complete summary") + '"}}]}\n\n'
        if request.url.host != "100.64.0.2": content += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=content, headers={"x-potluck-model-lock": "1", "x-potluck-model-sha256": PIN["sha256"], "x-potluck-batch-protocol": "1"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    executor = batch.HouseholdExecutor()
    async def workers(_):
        return [{"key": str(n), "name": str(n), "peer": {"mesh_ip": f"100.64.0.{n}"}} for n in (2, 3)]
    executor.workers = workers
    async def run():
        manager = batch.BatchManager(tmp_path / "private", executor)
        job = await settled(manager, manager.submit(submission(1))["id"])
        assert job["state"] == "completed"
        assert job["items"][0]["output"] == "complete summary"
        assert len(calls) == 2 and calls[0][1] == calls[1][1]
        assert "discard me" not in json.dumps(job)
        await manager.close()
    asyncio.run(run())


def test_private_storage_refuses_symlinks_and_shared_permissions(tmp_path):
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir(mode=0o700)
    link = tmp_path / "link"; link.symlink_to(elsewhere)
    with pytest.raises(batch.BatchError): batch.BatchStore(link)
    directory = tmp_path / "private"; directory.mkdir(mode=0o700)
    (directory / "jobs.sqlite3").symlink_to(tmp_path / "missing")
    with pytest.raises(batch.BatchError): batch.BatchStore(directory)
    if os.name != "nt":
        shared = tmp_path / "shared"; shared.mkdir(mode=0o755)
        with pytest.raises(batch.BatchError): batch.BatchStore(shared)


@pytest.mark.parametrize("changes", [
    {"scope": "circle"}, {"scope": "pool"}, {"concurrency": 9}, {"concurrency": True},
    {"max_tokens": 9999}, {"files": []}, {"files": [{"name": "a", "text": "\0"}]},
    {"files": [{"name": "../private", "text": "x"}]},
    {"files": [{"name": "a", "text": "a" * (16 * 1024 + 1)}]},
    {"files": [{"name": "a", "text": "x" * 16384}] * 17},
    {"model": {"id": "tiny", "sha256": "bad"}}, {"tools": [{"name": "shell"}]},
])
def test_request_bounds(changes):
    with pytest.raises(ValueError): submission(**changes)


def test_batch_routes_require_local_control_auth(monkeypatch):
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "private-token")
    app = create_app()
    local = TestClient(app, client=("127.0.0.1", 2345))
    assert local.get("/batches").status_code == 401
    assert local.get("/batches", headers={"Authorization": "Bearer private-token"}).status_code == 503
    remote = TestClient(app, client=("100.64.0.2", 2345))
    assert remote.get("/batches", headers={"Authorization": "Bearer private-token"}).status_code == 403
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN")
    assert TestClient(create_app(), client=("127.0.0.1", 2345)).get("/batches").status_code == 503


@pytest.mark.parametrize("reason", ["stop", "length", None])
def test_real_peer_batch_protocol_preserves_finish_reason(monkeypatch, fake_mesh, reason):
    from types import SimpleNamespace
    from potluck_local.routers import peer
    class Runtime:
        def is_loaded(self): return True
        def current_model_id(self): return "tiny"
        def assert_model_identity(self, model, sha): assert (model, sha) == ("tiny", PIN["sha256"])
        async def stream_chat_raw(self, messages, **kwargs):
            assert kwargs["expected_sha256"] == PIN["sha256"]
            yield {"choices": [{"delta": {"content": "summary"}, "finish_reason": None}]}
            if reason: yield {"choices": [{"delta": {}, "finish_reason": reason}]}
    async def no_meter(*_): pass
    monkeypatch.setattr(peer, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(peer, "get_settings", lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))
    monkeypatch.setattr(peer, "_record_served", no_meter)
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", "token")
    # A household peer: listed as reachable by this machine's mesh sidecar and
    # dialing this machine's own mesh address.
    fake_mesh.add_peer("100.64.0.2")
    client = TestClient(create_app(), client=("100.64.0.2", 1), base_url=fake_mesh.base_url)
    response = client.post("/peer/chat/completions", json={"model": "tiny", "potluck_model_sha256": PIN["sha256"],
                          "potluck_batch": True, "messages": [{"role": "user", "content": "text"}]})
    assert response.status_code == 200 and response.headers["x-potluck-batch-protocol"] == "1"
    async def parse():
        from potluck_local.services.remote_stream import openai_chunks
        wire = httpx.Response(200, content=response.content)
        if reason is None:
            with pytest.raises(RemoteStreamError):
                return [chunk async for chunk in openai_chunks(wire, "tiny", require_finish=True)]
        else:
            chunks = [chunk async for chunk in openai_chunks(wire, "tiny", require_finish=True)]
            assert chunks[-1]["choices"][0]["finish_reason"] == reason
    asyncio.run(parse())


def test_three_failures_exhaust_budget_and_output_truncation_does_not_disable_worker(tmp_path):
    async def run():
        executor = Executor(); executor.fail = {"0", "1", "2"}
        manager = batch.BatchManager(tmp_path / "private", executor)
        job = await settled(manager, manager.submit(submission(1))["id"])
        assert job["state"] == "failed" and len(executor.calls) == 3
        assert len(job["items"][0]["attempts"]) == 3
        executor.fail.clear()
        original = executor.complete
        async def truncated(job, item, worker):
            if item["id"] == "1": raise RemoteStreamError("limit", "output_truncated")
            return await original(job, item, worker)
        executor.complete = truncated; executor.available = WORKERS[:1]
        job = await settled(manager, manager.submit(submission(2))["id"])
        assert job["state"] == "failed"
        assert job["items"][0]["error"] == "output_truncated"
        assert job["items"][1]["state"] == "completed"
        await manager.close()
    asyncio.run(run())
