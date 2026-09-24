"""Disposable GPU probe. Native driver faults must not run in the sidecar."""
from __future__ import annotations
import asyncio
import json
import math
import subprocess
import sys
import threading
import time

PROBE_TIMEOUT = 30
_gate = threading.Lock()


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


def main(device: str) -> None:
    if device not in {"cpu", "mps", "cuda", "dml"}:
        raise SystemExit(2)
    # Probes contain no user content and never need a core dump.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, OSError, ValueError):
        pass
    print(json.dumps(_run_compute_probe(device)), flush=True)


async def compute_probe(device: str) -> tuple[bool, float, str]:
    if not _gate.acquire(blocking=False):
        return False, 0.0, "GPU self-test is already running; try again shortly"
    child = None
    try:
        command = ([sys.executable, "--gpu-probe", device] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "potluck_local.services.gpu_probe", device])
        child = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, limit=8192)
        async with asyncio.timeout(PROBE_TIMEOUT):
            line = await child.stdout.readline()
            await child.wait()
        if child.returncode != 0:
            return False, 0.0, "GPU self-test process failed; the runtime is still available"
        ok, latency, detail = json.loads(line)
        if not isinstance(ok, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) \
                or latency < 0 or not isinstance(detail, str) or len(detail) > 2048:
            raise ValueError()
        return ok, latency, detail
    except TimeoutError:
        return False, 0.0, "GPU self-test timed out; check the device driver"
    except (OSError, ValueError, TypeError):
        return False, 0.0, "GPU self-test could not complete; check the device backend"
    finally:
        # Also reap on client disconnect (AnyIO uses repeated cancellation).
        try:
            if child is not None:
                import anyio
                with anyio.CancelScope(shield=True):
                    if child.returncode is None:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            pass
                    await child.wait()
        finally:
            _gate.release()


if __name__ == "__main__":
    main(sys.argv[1])
