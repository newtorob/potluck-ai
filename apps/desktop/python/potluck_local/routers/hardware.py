"""Hardware detection router.

GET /hardware — return OS, CPU, RAM, GPU info for model recommendations.
"""

from __future__ import annotations

from fastapi import APIRouter

from potluck_local.services.hardware_detect import detect_hardware

router = APIRouter()


@router.get("")
async def get_hardware() -> dict:
    return detect_hardware()
