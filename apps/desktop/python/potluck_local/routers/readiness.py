"""GPU/runtime self-test endpoint — proves the device path actually computes.

GET /readiness/gpu-test

Two modes, chosen by what is available at call time:
  - inference  A model is loaded in the runtime: run a 1-token generation and time it.
  - compute    No model loaded: run a bounded tensor probe in a disposable process.
  - none       torch is absent AND no model is loaded: nothing can be verified.

Response shape (never 500s on device error — a failed probe is a valid result):
  { "ok": bool, "device": str, "ran": "inference|compute|none",
    "latency_ms": float, "detail": str }
"""

from __future__ import annotations

import asyncio
import platform
import time

from fastapi import APIRouter

router = APIRouter()


def _detect_device_name() -> str:
    """Return the torch device string the runtime would use, or 'cpu' as fallback.

    Mirrors pipeline_parallel.resolve_device / pick_device, but lazy: torch is
    only imported here so the hardware_detect result guides the label even when
    torch is not installed yet.
    """
    from potluck_local.services.hardware_detect import detect_hardware

    hw = detect_hardware()
    gpu_name: str | None = hw.get("gpu_name")  # type: ignore[assignment]
    os_name: str = hw.get("os", platform.system())  # type: ignore[assignment]
    arch: str = hw.get("arch", platform.machine())  # type: ignore[assignment]

    if os_name == "Darwin" and arch == "arm64":
        return "mps"

    if gpu_name:
        gpu_lower = (gpu_name or "").lower()
        # NVIDIA
        if any(n in gpu_lower for n in ("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx")):
            return "cuda"
        # AMD or Intel on Windows -> DirectML
        if os_name == "Windows":
            return "dml"

    return "cpu"


async def _run_inference_probe(runtime) -> tuple[bool, float, str]:
    """Use normal admission and cancellation, never a raw model handle."""
    import json
    from contextlib import aclosing

    from potluck_local.services.inference_errors import GPU_OUT_OF_MEMORY, GPU_OUT_OF_MEMORY_TEXT

    t0 = time.monotonic()
    completed = False
    try:
        async with aclosing(runtime.stream_chat(
            [{"role": "user", "content": "1"}], max_tokens=1, temperature=0.0,
        )) as stream:
            async for event in stream:
                for line in event.splitlines():
                    if line == "data: [DONE]":
                        completed = True
                    if line.startswith("data: {") and "error" in json.loads(line[6:]):
                        if json.loads(line[6:]).get("category") == GPU_OUT_OF_MEMORY:
                            return False, 0.0, GPU_OUT_OF_MEMORY_TEXT
                        return False, 0.0, "Loaded-model self-test failed"
        return completed, round((time.monotonic() - t0) * 1000, 2), (
            "1-token inference completed" if completed else "Loaded-model self-test was interrupted")
    except Exception:
        return False, round((time.monotonic() - t0) * 1000, 2), "Loaded-model self-test failed"


@router.get("/readiness/gpu-test")
async def gpu_test() -> dict:
    """Self-test: prove the GPU/runtime device actually computes.

    - If a model is loaded: runs a 1-token generation and times it.
    - Else: runs a tiny matmul in a bounded, isolated device-probe process.
    - If torch is absent and no model is loaded: returns ok=false with guidance.

    Never 500s on a device failure — a failed probe is a valid, useful result.
    """
    from potluck_local.services.runtime import get_runtime

    runtime = get_runtime()

    loop = asyncio.get_running_loop()

    if runtime.is_loaded():
        ok, latency_ms, detail = await _run_inference_probe(runtime)
        # Try to name the device from the loaded backend.
        device = _infer_loaded_device(runtime)
        return {
            "ok": ok,
            "device": device,
            "ran": "inference",
            "latency_ms": latency_ms,
            "detail": detail,
            # The 1-token probe above only shows that a token came out. This is
            # whether the answers are right (services/acceptance.py).
            "acceptance": runtime.acceptance_summary(),
        }

    # No model loaded — try a raw compute probe.
    device_str = await loop.run_in_executor(None, _detect_device_name)

    try:
        import importlib.util
        torch_available = importlib.util.find_spec("torch") is not None
    except Exception:
        torch_available = False

    if not torch_available:
        return {
            "ok": False,
            "device": device_str,
            "ran": "none",
            "latency_ms": 0.0,
            "detail": "install the inference backend or load a model to verify the GPU",
        }

    from potluck_local.services.gpu_probe import compute_probe
    ok, latency_ms, detail = await compute_probe(device_str)
    return {
        "ok": ok,
        "device": device_str,
        "ran": "compute",
        "latency_ms": latency_ms,
        "detail": detail,
    }


def _infer_loaded_device(runtime) -> str:
    """Best-effort device label for an already-loaded model."""
    # Pipeline/whole-model path exposes .device on the engine.
    if runtime._pipeline is not None:
        engine = runtime._pipeline
        dev = getattr(engine, "device", None)
        if dev is not None:
            return str(dev)

    # llama.cpp: ask hardware_detect what device class it is.
    return _detect_device_name()
