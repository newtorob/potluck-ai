"""Hardware detection router.

GET /hardware - return OS, CPU, RAM, GPU info for model recommendations.

`detect_hardware()` calls subprocess.run(system_profiler) on macOS which
can block for up to 10 s on the first invocation.  We off-load it to the
default thread-pool executor so the uvicorn event loop is never stalled.
The result is cached in the peer module's `_hardware_cache`; the second
call is free.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from potluck_local.services.hardware_detect import detect_hardware

router = APIRouter()


@router.get("")
async def get_hardware() -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, detect_hardware)
