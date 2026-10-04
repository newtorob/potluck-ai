"""Serialize user model commands with the runtime's one-shot startup restore.

The runtime already drains inference/native workers during a swap. This outer
gate also covers file validation/deletion and saving the restart choice, so a
queued unload cannot finish before a startup load and then be undone by it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import HTTPException

from potluck_local.services.settings_manager import get_settings, save_settings

logger = logging.getLogger(__name__)
AUTOLOAD_KEY = "potluck.autoload_on_startup"  # existing durable app preference


class ModelLifecycle:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._manual_requested = False
        self._stopping = False
        self._status = "idle"
        self._model_id: str | None = None

    def status(self) -> dict:
        return {"status": self._status, "model_id": self._model_id}

    @asynccontextmanager
    async def command(self):
        self._manual_requested = True
        async with self._lock:
            if self._stopping:
                raise HTTPException(503, "Runtime is shutting down.")
            self._status, self._model_id = "idle", None
            yield

    @staticmethod
    def remember(slug: str | None) -> None:
        # Re-read AFTER the potentially long load; don't overwrite settings
        # changed during it. Copy the cached model so a failed save is not cached.
        settings = get_settings().model_copy(deep=True)
        if settings.runtime.startup_model_id != slug:
            settings.runtime.startup_model_id = slug
            save_settings(settings)

    @staticmethod
    def forget(slug: str) -> None:
        if get_settings().runtime.startup_model_id == slug:
            ModelLifecycle.remember(None)

    async def load_and_remember(self, operation: Awaitable[dict], slug: str | None) -> dict:
        async def transaction() -> dict:
            result = await operation
            self.remember(slug)
            return result

        task = asyncio.create_task(transaction())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A disconnected caller cannot stop native loading. Keep the command
            # gate until both the load and its durable choice have settled.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def restore(self, load: Callable[[str], Awaitable[dict]]) -> None:
        async with self._lock:
            if self._manual_requested or self._stopping:
                return
            settings = get_settings()
            if settings.ui.get(AUTOLOAD_KEY) == "false":
                self._status = "disabled"
                return
            slug = settings.runtime.startup_model_id
            if not slug:
                return
            self._status, self._model_id = "loading", slug
            try:
                # Same path checks, integrity checks and resource settings as a
                # manual local load. Never download or reactivate remote peers.
                await load(slug)
            except asyncio.CancelledError:
                self._status = "cancelled"
                raise  # runtime drains its native worker before releasing access
            except Exception:
                self._status = "failed"
                logger.exception("model_restore_failed slug=%s", slug)
            else:
                self._status = "restored"
                logger.info("model_restored slug=%s", slug)

    async def stop(self) -> None:
        self._stopping = True
        # No new commands/restore may start after shutdown's final unload.
        async with self._lock:
            pass


_lifecycle: ModelLifecycle | None = None


def get_model_lifecycle() -> ModelLifecycle:
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = ModelLifecycle()
    return _lifecycle
