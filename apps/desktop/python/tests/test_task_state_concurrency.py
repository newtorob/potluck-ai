"""Regression test — 2026-07-31 P2 sidecar audit, finding #8.

TaskStateManager.update_task() did an UNLOCKED read (get_task) -> full-row
merge -> write. Two concurrent callers patching different fields on the same
task each read the row before either had written, so whichever write landed
second silently reverted the other caller's change — a classic lost update.
complete_step() also unconditionally appended the step to completed_steps
even when it was no longer in pending_steps, so retrying a completion (e.g.
after a client-side timeout/retry) produced a duplicate entry
(["step", "step"]) instead of a no-op.

The fix:
- update_task()/complete_step()/add_error() all serialize their
  read-merge-write through a per-task_id asyncio.Lock (TaskStateManager._lock_for),
  so a read for task X can never observe a state that a concurrent writer for
  the same X is about to overwrite without first committing.
- complete_step() is now a no-op (returns the task unchanged, no DB write)
  when the step isn't currently pending — completing an already-completed
  step twice no longer duplicates it.

Isolated via POTLUCK_MEMORY_DB_PATH + POTLUCK_DATA_DIR so this test can never
touch a real ~/.potluck.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.task_state import TaskStateManager, get_task_manager

LOCAL = ("127.0.0.1", 50000)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data_dir"))
    _run(init_memory_db(db_path))
    return db_path


# ---------------------------------------------------------------------------
# Concurrent field updates must not lose each other's changes.
# ---------------------------------------------------------------------------


def test_concurrent_update_task_calls_do_not_lose_a_field(isolated_env, monkeypatch):
    manager = get_task_manager()

    async def scenario():
        task = await manager.create_task(task_name="ship the fix", objective="close the P2s")

        # Widen the read-merge-write race window deterministically: the
        # FIRST get_task() call (from whichever update_task call acquires
        # the lock/starts first) pauses briefly right after its read,
        # before returning to its caller's merge step — exactly the window
        # in which the unlocked implementation let a second concurrent
        # caller's read-modify-write interleave and get clobbered.
        real_get_task = TaskStateManager.get_task
        call_count = 0

        async def _instrumented_get_task(self, task_id):
            nonlocal call_count
            call_count += 1
            n = call_count
            result = await real_get_task(self, task_id)
            if n == 1:
                await asyncio.sleep(0.05)
            return result

        monkeypatch.setattr(TaskStateManager, "get_task", _instrumented_get_task)

        await asyncio.gather(
            manager.update_task(task.id, {"metadata": {"x": 1}}),
            manager.update_task(task.id, {"context_summary": "hello"}),
        )

        final = await real_get_task(manager, task.id)
        return final

    final = _run(scenario())
    # Both concurrent writers' changes must be present — neither lost.
    assert final.metadata == {"x": 1}
    assert final.context_summary == "hello"


# ---------------------------------------------------------------------------
# complete_step() idempotency.
# ---------------------------------------------------------------------------


def test_completing_an_already_completed_step_is_a_noop(isolated_env):
    async def scenario():
        manager = get_task_manager()
        task = await manager.create_task(
            task_name="ship the fix", objective="close the P2s",
            pending_steps=["write the tests"],
        )

        first = await manager.complete_step(task.id, "write the tests")
        assert first.pending_steps == []
        assert first.completed_steps == ["write the tests"]

        # Retry the exact same completion (the auditor's repro: a client
        # retries after e.g. a dropped response).
        second = await manager.complete_step(task.id, "write the tests")
        return first, second

    first, second = _run(scenario())
    assert second.pending_steps == []
    # Must stay a single entry, not ["write the tests", "write the tests"].
    assert second.completed_steps == ["write the tests"]


def test_completing_a_step_that_was_never_pending_is_a_noop(isolated_env):
    async def scenario():
        manager = get_task_manager()
        task = await manager.create_task(
            task_name="t", objective="o", pending_steps=["a"],
        )
        return await manager.complete_step(task.id, "never-existed")

    result = _run(scenario())
    assert result.completed_steps == []
    assert result.pending_steps == ["a"]


def test_route_level_double_complete_step_does_not_duplicate(isolated_env):
    app = create_app()
    with TestClient(app, client=LOCAL) as client:
        create_resp = client.post(
            "/tasks",
            json={
                "task_name": "ship the fix",
                "objective": "close the P2s",
                "pending_steps": ["write the tests"],
            },
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["id"]

        r1 = client.post(f"/tasks/{task_id}/complete-step", json={"step": "write the tests"})
        assert r1.status_code == 200
        assert r1.json()["completed_steps"] == ["write the tests"]

        r2 = client.post(f"/tasks/{task_id}/complete-step", json={"step": "write the tests"})
        assert r2.status_code == 200
        assert r2.json()["completed_steps"] == ["write the tests"]
