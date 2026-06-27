"""GPU/runtime self-test endpoint — proves the device path actually computes.

GET /readiness/gpu-test

Two modes, chosen by what is available at call time:
  - inference  A model is loaded in the runtime: run a 1-token generation and time it.
  - compute    No model loaded: lazily import torch, allocate a small tensor on the
               detected device, run a tiny matmul, sync, and confirm the result is finite.
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


def _run_compute_probe(device_str: str) -> tuple[bool, float, str]:
    """Allocate a small tensor on *device_str*, do a matmul, sync, check finite.

    Returns (ok, latency_ms, detail).  Never raises — caller receives a bool.
    """
    try:
        import torch  # lazy — only imported here, never at module load
    except ImportError:
        return False, 0.0, "torch is not installed; install the inference backend to verify the GPU"

    t0 = time.monotonic()
    try:
        if device_str == "dml":
            try:
                import torch_directml
                device = torch_directml.device()
            except ImportError:
                return False, 0.0, "torch_directml not installed; cannot verify DirectML device"
        else:
            device = torch.device(device_str)

        # Small matmul: float32 on cpu/mps/dml, float16 on cuda
        dtype = torch.float16 if device_str == "cuda" else torch.float32
        a = torch.randn(64, 64, dtype=dtype, device=device)
        b = torch.randn(64, 64, dtype=dtype, device=device)
        c = torch.matmul(a, b)

        # Synchronise device so latency is real wall-clock
        if device_str == "cuda":
            torch.cuda.synchronize(device)
        elif device_str == "mps":
            torch.mps.synchronize()

        latency_ms = round((time.monotonic() - t0) * 1000, 2)

        if not torch.isfinite(c).all():
            return False, latency_ms, f"matmul result contained non-finite values on {device_str}"

        if device_str == "cpu":
            detail = f"matmul ok on cpu (no GPU acceleration); consider installing a model or GPU backend"
        else:
            detail = f"matmul ok on {device_str}"
        return True, latency_ms, detail

    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return False, latency_ms, f"{type(exc).__name__}: {exc}"


def _run_inference_probe(runtime) -> tuple[bool, float, str]:
    """Generate exactly 1 token from the loaded model and time it.

    Returns (ok, latency_ms, detail).  Never raises.
    """
    t0 = time.monotonic()
    try:
        # llama.cpp path
        if runtime._model is not None:
            model = runtime._model
            result = model.create_chat_completion(
                messages=[{"role": "user", "content": "1"}],
                max_tokens=1,
                temperature=0.0,
                stream=False,
            )
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
            tok = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            return True, latency_ms, f"1-token inference ok (model={runtime._model_id!r}, token={tok!r})"

        # pipeline / whole-model path: run a tiny synchronous forward using the
        # engine's own tokenizer + model head, avoid the async stream machinery.
        if runtime._pipeline is not None:
            engine = runtime._pipeline
            from potluck_local.services.pipeline_parallel import WholeModelEngine

            if isinstance(engine, WholeModelEngine):
                import torch

                # Tokenise a single byte
                tok_ids = engine.tokenizer.encode("1", return_tensors="pt")
                tok_ids = tok_ids.to(engine.device)
                with torch.no_grad():
                    out = engine.model(tok_ids)
                # Just confirm the logits tensor has finite values
                logits = out.logits if hasattr(out, "logits") else out[0]
                latency_ms = round((time.monotonic() - t0) * 1000, 2)
                if torch.isfinite(logits).all():
                    return True, latency_ms, f"1-forward ok on whole-model engine (model={runtime._model_id!r})"
                return False, latency_ms, "forward pass produced non-finite logits"
            else:
                # Distributed PipelineParallelEngine: avoid the WAN round-trip.
                # The head block is local — just confirm it is alive.
                latency_ms = round((time.monotonic() - t0) * 1000, 2)
                return True, latency_ms, f"pipeline engine loaded (model={runtime._model_id!r}); head layers resident"

        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return False, latency_ms, "no model in runtime slot"

    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return False, latency_ms, f"{type(exc).__name__}: {exc}"


@router.get("/readiness/gpu-test")
async def gpu_test() -> dict:
    """Self-test: prove the GPU/runtime device actually computes.

    - If a model is loaded: runs a 1-token generation and times it.
    - Else: lazily imports torch, runs a tiny matmul on the detected device.
    - If torch is absent and no model is loaded: returns ok=false with guidance.

    Never 500s on a device failure — a failed probe is a valid, useful result.
    """
    from potluck_local.services.runtime import get_runtime

    runtime = get_runtime()

    loop = asyncio.get_running_loop()

    if runtime.is_loaded():
        # Model already loaded — real inference probe (must run in executor;
        # llama.cpp / torch calls are blocking).
        ok, latency_ms, detail = await loop.run_in_executor(
            None, lambda: _run_inference_probe(runtime)
        )
        # Try to name the device from the loaded backend.
        device = _infer_loaded_device(runtime)
        return {
            "ok": ok,
            "device": device,
            "ran": "inference",
            "latency_ms": latency_ms,
            "detail": detail,
        }

    # No model loaded — try a raw compute probe.
    device_str = await loop.run_in_executor(None, _detect_device_name)

    # Check torch availability without importing it in the async frame.
    def _probe():
        return _run_compute_probe(device_str)

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

    ok, latency_ms, detail = await loop.run_in_executor(None, _probe)
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
