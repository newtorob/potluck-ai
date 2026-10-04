"""Restart selection, command races and failure isolation (no real weights/state)."""
import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException

from potluck_local.routers import health, models
from potluck_local.services import model_lifecycle, runtime
from potluck_local.services.model_lifecycle import AUTOLOAD_KEY, ModelLifecycle
from potluck_local.services.settings_manager import get_settings, save_settings


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POTLUCK_VERIFY_MODEL_ON_LOAD", raising=False)
    lifecycle = ModelLifecycle()
    monkeypatch.setattr(model_lifecycle, "_lifecycle", lifecycle)
    rt = runtime.LLMRuntime()
    monkeypatch.setattr(runtime, "_runtime", rt)
    monkeypatch.setattr(models.model_registry, "get_manifest", lambda slug: None)
    calls = []

    def load(path, **kwargs):
        slug = Path(path).parent.name
        calls.append((slug, kwargs))
        rt._model, rt._model_id = object(), slug
        return {"model_id": slug}

    def unload():
        rt._model, rt._model_id = None, None

    monkeypatch.setattr(rt, "load_model", load)
    monkeypatch.setattr(rt, "unload_model", unload)
    for slug in ("first", "second"):
        folder = tmp_path / "models" / slug
        folder.mkdir(parents=True)
        (folder / "model.gguf").write_bytes(b"test weights")
    return lifecycle, rt, calls


def test_success_restores_with_current_resources_and_preserves_other_settings(rig, tmp_path):
    lifecycle, rt, calls = rig

    async def scenario():
        await models.load_model("first")
        assert json.loads((tmp_path / "config.json").read_text())["runtime"]["startup_model_id"] == "first"
        # Shutdown releases memory, but keeps the next-start choice.
        await lifecycle.stop()
        await rt.unload_model_async()
        settings = get_settings().model_copy(deep=True)
        settings.runtime.context_size, settings.runtime.threads = 512, 2
        settings.runtime.gpu_layers = 0
        settings.privacy.chat_history_enabled = True
        settings.active_model_id = "unrelated-remote-preference"
        save_settings(settings)
        restarted = ModelLifecycle()
        await restarted.restore(models._load_model)
        assert restarted.status() == {"status": "restored", "model_id": "first"}
        assert rt.current_model_id() == "first"
        assert get_settings().privacy.chat_history_enabled
        assert get_settings().active_model_id == "unrelated-remote-preference"
        assert calls[-1][1] == {"n_ctx": 512, "n_gpu_layers": 0, "n_threads": 2, "rpc_servers": None}

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["unload", "delete"])
def test_explicit_removal_stays_unloaded_after_restart(rig, action):
    lifecycle, rt, calls = rig

    async def scenario():
        await models.load_model("first")
        await getattr(models, f"{action}_model")("first")
        assert get_settings().runtime.startup_model_id is None
        await ModelLifecycle().restore(models._load_model)
        assert not rt.is_loaded()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_failed_load_keeps_previous_choice(rig, monkeypatch):
    lifecycle, rt, _ = rig
    lifecycle.remember("first")

    async def fail(*args, **kwargs):
        raise ValueError("bad weights")

    monkeypatch.setattr(rt, "load_model_async", fail)
    with pytest.raises(ValueError):
        asyncio.run(models.load_model("second"))
    assert get_settings().runtime.startup_model_id == "first"


@pytest.mark.parametrize("slug", ["missing", "..", "../outside", "first"])
def test_restore_failure_keeps_control_alive_without_fallback(rig, monkeypatch, slug):
    lifecycle, rt, calls = rig
    lifecycle.remember(slug)

    async def fail(*args, **kwargs):
        raise ValueError("corrupt weights")

    monkeypatch.setattr(rt, "load_model_async", fail)

    async def scenario():
        await lifecycle.restore(models._load_model)
        result = await health.health()
        assert result["status"] == "ok"
        assert result["model_restore"] == {"status": "failed", "model_id": slug}
        assert not result["model_loaded"]
        # Even a missing/corrupt saved model can be explicitly forgotten.
        await models.unload_model(slug)
        assert get_settings().runtime.startup_model_id is None

    asyncio.run(scenario())
    assert not calls


def test_integrity_check_also_applies_to_restore(rig, monkeypatch):
    lifecycle, rt, calls = rig
    lifecycle.remember("first")
    monkeypatch.setenv("POTLUCK_VERIFY_MODEL_ON_LOAD", "1")
    monkeypatch.setattr(models.model_registry, "get_manifest", lambda slug: {"sha256": "0" * 64})
    asyncio.run(lifecycle.restore(models._load_model))
    assert lifecycle.status()["status"] == "failed"
    assert not calls


def test_opt_out_and_no_saved_local_model_never_choose_a_fallback(rig):
    lifecycle, rt, calls = rig

    async def scenario():
        # Installed weights + an agent preference are not restoration consent.
        settings = get_settings().model_copy(deep=True)
        settings.active_model_id = "first"
        save_settings(settings)
        await lifecycle.restore(models._load_model)
        assert not calls
        lifecycle.remember("first")
        settings = get_settings().model_copy(deep=True)
        settings.ui[AUTOLOAD_KEY] = "false"
        save_settings(settings)
        await lifecycle.restore(models._load_model)
        assert lifecycle.status()["status"] == "disabled"
        assert not calls
        assert get_settings().runtime.startup_model_id == "first"

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    models.LoadModelRequest(rpc_servers=["100.64.0.2:50052"]),
    models.LoadModelRequest(pipeline_auto={}),
])
def test_distributed_switch_clears_local_restart_choice(rig, monkeypatch, body):
    lifecycle, rt, calls = rig
    lifecycle.remember("first")

    async def load(*args, **kwargs):
        return {"model_id": "second"}

    monkeypatch.setattr(models, "_load_model", load)
    asyncio.run(models.load_model("second", body))
    assert get_settings().runtime.startup_model_id is None


def test_manual_command_before_startup_wins(rig):
    lifecycle, rt, calls = rig
    lifecycle.remember("first")

    async def scenario():
        await models.load_model("second")
        await lifecycle.restore(models._load_model)
        assert [slug for slug, _ in calls] == ["second"]
        assert get_settings().runtime.startup_model_id == "second"

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["unload", "delete", "load"])
def test_command_during_restore_wins_and_health_remains_responsive(rig, monkeypatch, command):
    lifecycle, rt, calls = rig
    lifecycle.remember("first")

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        original = rt.load_model_async

        async def slow_load(*args, **kwargs):
            entered.set()
            await finish.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(rt, "load_model_async", slow_load)
        restore = asyncio.create_task(lifecycle.restore(models._load_model))
        await entered.wait()
        status = await asyncio.wait_for(health.health(), .2)
        assert status["model_restore"]["status"] == "loading"
        target = "second" if command == "load" else "first"
        request = asyncio.create_task(getattr(models, f"{command}_model")(target))
        await asyncio.sleep(0)
        assert not request.done()
        # Settings written during load must survive saving a new model choice.
        settings = get_settings().model_copy(deep=True)
        settings.privacy.chat_history_enabled = True
        save_settings(settings)
        finish.set()
        await asyncio.gather(restore, request)
        expected = "second" if command == "load" else None
        assert rt.current_model_id() == expected
        assert get_settings().runtime.startup_model_id == expected
        assert get_settings().privacy.chat_history_enabled

    asyncio.run(scenario())


@pytest.mark.parametrize("restoring", [True, False])
def test_cancelled_native_load_drains_before_shutdown_or_unload(rig, monkeypatch, restoring):
    lifecycle, rt, calls = rig
    lifecycle.remember("first")
    entered, finish = threading.Event(), threading.Event()
    original = rt.load_model

    def slow_load(*args, **kwargs):
        entered.set()
        assert finish.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(rt, "load_model", slow_load)

    async def scenario():
        operation = lifecycle.restore(models._load_model) if restoring else models.load_model("second")
        task = asyncio.create_task(operation)
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()  # repeat cancellation cannot release the gate either
        assert not task.done()
        waiter = asyncio.create_task(lifecycle.stop())
        await asyncio.sleep(.02)
        assert not waiter.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await waiter
        # A cancelled manual request still persists a completed native load.
        assert get_settings().runtime.startup_model_id == ("first" if restoring else "second")
        await rt.unload_model_async()
        assert not rt.is_loaded()
        with pytest.raises(HTTPException) as exc:
            await models.load_model("first")
        assert exc.value.status_code == 503

    try:
        asyncio.run(scenario())
    finally:
        finish.set()
