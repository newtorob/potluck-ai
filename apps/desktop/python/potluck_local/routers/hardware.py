"""Hardware detection router.

GET /hardware — return OS, CPU, RAM, GPU info for model recommendations.
"""

from __future__ import annotations

import platform

from fastapi import APIRouter

from potluck_local.services.hardware_detect import detect_hardware

router = APIRouter()


@router.get("")
async def get_hardware() -> dict[str, object]:
    """Return hardware capabilities detected on the host machine.

    In the scaffold phase this always sets detected=True once hardware_detect
    runs; the GPU path will be enhanced in a subsequent task.
    """
    info = detect_hardware()
    # Annotate with a top-level 'detected' flag so the frontend can tell
    # whether the detection ran successfully vs. returned empty stubs.
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "detected": True,
        **info,
    }
