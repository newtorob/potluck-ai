"""Fit the pinned engine to current memory, including shared-memory workers."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import subprocess

import psutil

from .split_native import SplitError, binary

MIB = 1024 ** 2
RESERVE_MIB = 2048
DEVICE = re.compile(r"^\s*(CUDA\d+|MTL\d+|Vulkan\d+|RPC\d+): (.+) "
                    r"\((\d+) MiB, (\d+) MiB free\)\s*$")
# ggml-vulkan prints this per device at debug level; uma is 1 for an integrated GPU.
VULKAN_INFO = re.compile(r"ggml_vulkan: (\d+) = .* \| uma: ([01]) \|")
_unified_memory: dict[str, bool] = {}


def shared_memory_budget(available: object) -> dict | None:
    """A startup estimate shared by planning and native fit, never a reservation."""
    if type(available) is not int or available <= 0:
        return None
    available_mib = available // MIB
    return {"availableMiB": available_mib, "reserveMiB": RESERVE_MIB,
            "budgetMiB": max(0, available_mib - RESERVE_MIB)}


def fit_arguments(output: str, workers: dict[str, dict], local_available: int,
                  local_unified: bool | None = None) -> list[str]:
    """Explicit device ordering keeps fit margins attached to the right worker.

    Metal's native free count excludes other apps' RAM. CPU RPC workers share
    system RAM too. Bound each machine's aggregate allocation using the fresh
    lease snapshot, with 2 GiB reserved. CUDA uses native free VRAM with 1 GiB
    headroom. Vulkan is CUDA on a dedicated GPU and metal on one that shares
    system memory (unifiedMemory on a worker, local_unified on the head). An
    unavailable budget fails closed before loading model weights.

    Workers come first and the head's own devices last. llama.cpp puts the
    output layer, and so the full logits vector every token, on the last device,
    and that should be the head rather than a worker across the network
    (measured 2026-10-20: 4.67 tok/s with the head first, 11.62 tok/s last).
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
    devices.sort(key=lambda d: d[1] == "local")  # stable: workers keep their order
    margins = []
    for name, key, free in devices:
        worker = workers.get(key)
        backend = worker.get("backend") if worker else (
            "metal" if name.startswith("MTL") else "vulkan" if name.startswith("Vulkan") else "cuda")
        if backend not in ("metal", "cpu", "cuda", "vulkan"):
            raise SplitError("Worker did not report a supported memory budget. Update its Potluck runtime.")
        unified = worker.get("unifiedMemory") if worker else local_unified
        if backend == "vulkan" and type(unified) is not bool:
            raise SplitError("Worker did not report a supported memory budget. Update its Potluck runtime.")
        margin = 1024
        if backend in ("metal", "cpu") or backend == "vulkan" and unified:
            available = worker.get("freeMemoryBytes") if worker else local_available
            memory = shared_memory_budget(available)
            if memory is None:
                raise SplitError("Worker did not report available memory. Update its Potluck runtime.")
            count = sum(k == key for _, k, _ in devices)
            budget = memory["budgetMiB"] // count
            if budget < 256:
                raise SplitError("A split computer has too little available memory. Close other models or apps and try again.")
            margin = max(margin, free - budget)
        margins.append(str(margin))
    return ["--device", ",".join(d[0] for d in devices), "--fit", "on",
            "--fit-target", ",".join(margins)]


async def _list_devices(root, *args: str, merge_stderr=False) -> bytes:
    # List-devices makes no model allocations and does not receive any prompts.
    # It connects only to our leased loopback tunnels; never arbitrary URLs.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_", "GGML_RPC_"))}
    process = None
    try:
        process = await asyncio.create_subprocess_exec(str(binary(root, "llama-server")), *args,
            "--list-devices", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL, env=env)
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
    return output


def known_unified_memory(root) -> bool | None:
    return _unified_memory.get(str(root))


async def probe_unified_memory(root) -> bool:
    """Whether this machine's Vulkan devices share system memory, read once per engine.

    The pinned ggml-vulkan logs `uma` per device only at debug level, so the
    probe asks for it and reads both streams. Any device that shares counts, and
    a probe that finds no device line fails closed.
    """
    key = str(root)
    if key not in _unified_memory:
        output = await _list_devices(root, "--log-verbosity", "5", merge_stderr=True)
        found = VULKAN_INFO.findall(output[:1048576].decode("utf-8", errors="replace"))
        if len(output) > 1048576 or not found:
            raise SplitError("Could not tell whether this computer's GPU shares system memory.", 503)
        _unified_memory[key] = any(uma == "1" for _, uma in found)
    return _unified_memory[key]


async def probe_fit_arguments(root, endpoints: list[str], workers: dict[str, dict]) -> list[str]:
    output = await _list_devices(root, "--rpc", ",".join(endpoints))
    if len(output) > 16384:
        raise SplitError("Split engine returned an invalid device inventory.")
    text = output.decode("utf-8", errors="replace")
    local_unified = await probe_unified_memory(root) if re.search(r"^\s*Vulkan\d+:", text, re.MULTILINE) else None
    return fit_arguments(text, workers, psutil.virtual_memory().available, local_unified)
