"""Fit the pinned engine to current memory, including shared-memory workers."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import subprocess

import psutil

from .split_native import SplitError

MIB = 1024 ** 2
RESERVE_MIB = 2048
DEVICE = re.compile(r"^\s*(CUDA\d+|MTL\d+|RPC\d+): (.+) "
                    r"\((\d+) MiB, (\d+) MiB free\)\s*$")


def fit_arguments(output: str, workers: dict[str, dict], local_available: int) -> list[str]:
    """Explicit device ordering keeps fit margins attached to the right worker.

    Metal's native free count excludes other apps' RAM. CPU RPC workers share
    system RAM too. Bound each machine's aggregate allocation using the fresh
    lease snapshot, with 2 GiB reserved. CUDA uses native free VRAM with 1 GiB
    headroom. An unavailable budget fails closed before loading model weights.
    """
    devices = []
    for line in output.splitlines():
        match = DEVICE.fullmatch(line)
        if match:
            name, description, total, free = match.groups()
            port = re.fullmatch(r"127\.0\.0\.1:(\d+)", description)
            if name.startswith("RPC") and not port:
                raise SplitError("Split engine reported an unexpected worker address.")
            key = port[1] if name.startswith("RPC") else "local"
            if key != "local" and key not in workers:
                raise SplitError("Split engine reported an unexpected worker device.")
            devices.append((name, key, int(free)))
    if not devices or set(workers) - {key for _, key, _ in devices}:
        raise SplitError("Could not discover every selected split device. Check worker engine compatibility.")
    if len(devices) > 16 or len({name for name, _, _ in devices}) != len(devices):
        raise SplitError("Split engine reported an unsupported device layout.")
    margins = []
    for name, key, free in devices:
        worker = workers.get(key)
        backend = worker.get("backend") if worker else ("metal" if name.startswith("MTL") else "cuda")
        if backend not in ("metal", "cpu", "cuda"):
            raise SplitError("Worker did not report a supported memory budget. Update its Potluck runtime.")
        margin = 1024
        if backend in ("metal", "cpu"):
            available = worker.get("freeMemoryBytes") if worker else local_available
            if type(available) is not int or available <= 0:
                raise SplitError("Worker did not report available memory. Update its Potluck runtime.")
            count = sum(k == key for _, k, _ in devices)
            budget = (available // MIB - RESERVE_MIB) // count
            if budget < 256:
                raise SplitError("A split computer has too little available memory. Close other models or apps and try again.")
            margin = max(margin, free - budget)
        margins.append(str(margin))
    return ["--device", ",".join(d[0] for d in devices), "--fit", "on",
            "--fit-target", ",".join(margins)]


async def probe_fit_arguments(root, endpoints: list[str], workers: dict[str, dict]) -> list[str]:
    # List-devices makes no model allocations and does not receive any prompts.
    # It connects only to our leased loopback tunnels; never arbitrary URLs.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_", "GGML_RPC_"))}
    process = None
    try:
        process = await asyncio.create_subprocess_exec(str(root / "llama-server"), "--rpc", ",".join(endpoints),
            "--list-devices", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env)
        async with asyncio.timeout(20):
            output, _ = await process.communicate()
        if process.returncode:
            raise OSError()
    except (OSError, TimeoutError):
        raise SplitError("Could not read split device memory. Check worker connectivity and try again.", 503) from None
    finally:
        # Cancellation must not leave a native RPC probe or an executor thread
        # alive after the split/sidecar has stopped.
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
    if len(output) > 16384:
        raise SplitError("Split engine returned an invalid device inventory.")
    return fit_arguments(output.decode("utf-8", errors="replace"), workers, psutil.virtual_memory().available)
