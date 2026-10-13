"""Background tasks spawned by the app lifespan must be cancelled and drained on shutdown.

app.py's _lifespan() spawns the hardware-warm, embedding-backfill, and
stale-session-sweeper tasks. If they were spawned via bare
asyncio.create_task(...) without retaining a reference, nothing would cancel
or await them on shutdown, and a task still running when the process/event
loop went away (most visibly the stale-session sweeper, which loops forever on
asyncio.sleep(60)) would be abandoned - an aiosqlite worker could hit a
closed event loop after a test run tore its loop down while that sweeper was
still alive.

So every task spawned in _lifespan is added to a local
`background_tasks` set (via a small _track() helper using
task.add_done_callback to self-remove on completion). After the lifespan's
`yield` - i.e. on shutdown - every task still in that set is cancelled and
then awaited (asyncio.gather(..., return_exceptions=True)) before the
model-unload/GC step runs, so nothing is left pending when the loop this
lifespan belongs to starts closing.

This test drives potluck_local.app._lifespan directly as an async context
manager (not through uvicorn) so it can assert on asyncio.all_tasks()
before/after the `yield` boundary. The hardware-warm coroutine is
monkeypatched to hang forever (proving it actually gets cancelled, not just
"happened to finish already"); the stale-session sweeper's own infinite loop
provides a second, always-pending task with no mocking needed. Isolated via
POTLUCK_MEMORY_DB_PATH + POTLUCK_DATA_DIR so this test can never touch a
real ~/.potluck.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

import potluck_local.app as app_module
import potluck_local.routers.peer as peer_module


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    from potluck_local.services import model_lifecycle

    monkeypatch.setattr(model_lifecycle, "_lifecycle", model_lifecycle.ModelLifecycle())
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data_dir"))
    return db_path


async def _hang_forever() -> None:
    await asyncio.sleep(3600)


def test_invalid_batch_storage_keeps_runtime_available(isolated_env, monkeypatch):
    from potluck_local.services import batch_jobs
    def unavailable(*_):
        raise batch_jobs.BatchError("unsafe_storage", "Private content must not be logged", 503)
    monkeypatch.setattr(batch_jobs, "BatchManager", unavailable)
    async def scenario():
        app = FastAPI()
        async with app_module._lifespan(app):
            assert app.state.batch_manager is None
    asyncio.run(scenario())


def test_lifespan_cancels_and_drains_background_tasks_on_shutdown(isolated_env, monkeypatch):
    # Replace the (real, subprocess-backed) hardware warm-up with something
    # that never finishes on its own, so it can only go away via the
    # shutdown path's explicit cancellation.
    monkeypatch.setattr(peer_module, "_cached_hardware", _hang_forever)

    async def scenario() -> set[asyncio.Task]:
        dummy_app = FastAPI()
        outer_task = asyncio.current_task()

        async with app_module._lifespan(dummy_app):
            pending = {
                t for t in asyncio.all_tasks() if t is not outer_task and not t.done()
            }
            # At minimum: the hardware-warm hang + the periodic
            # stale-session sweeper (sleeps 60s in a loop, never exits on
            # its own) must both be alive as real background tasks.
            assert len(pending) >= 2, (
                f"expected >=2 live background tasks during lifespan, got {len(pending)}"
            )

        # Shutdown (the code after `yield`) already ran by the time the
        # `async with` block above exits. One tick lets any cancellation
        # callbacks settle.
        await asyncio.sleep(0)
        return {t for t in asyncio.all_tasks() if t is not outer_task and not t.done()}

    remaining = asyncio.run(scenario())
    assert remaining == set(), (
        f"{len(remaining)} background task(s) still pending after lifespan shutdown: "
        "they were not cancelled/drained before teardown"
    )
