import asyncio
import os
import sys

import pytest

from potluck_local.services.split_memory import fit_arguments, probe_fit_arguments, MIB
from potluck_local.services.split_native import SplitError


def test_mac_budget_uses_available_ram_instead_of_metal_capacity():
    output = "Available devices:\n  CUDA0: NVIDIA (10811 MiB, 10500 MiB free)\n  RPC0: 127.0.0.1:1234 (10922 MiB, 10922 MiB free)\n"
    args = fit_arguments(output, {"1234": {"backend": "metal", "freeMemoryBytes": 4096 * MIB}}, 60000 * MIB)
    assert args == ["--device", "RPC0,CUDA0", "--fit", "on", "--fit-target", "8874,1024"]
    assert "--n-gpu-layers" not in args  # permit CPU overflow while preserving context


def test_shared_budget_is_divided_across_devices_on_same_machine():
    output = "MTL0: Apple (10000 MiB, 10000 MiB free)\nRPC0: 127.0.0.1:1234 (8000 MiB, 8000 MiB free)\nRPC1: 127.0.0.1:1234 (8000 MiB, 8000 MiB free)"
    args = fit_arguments(output, {"1234": {"backend": "cpu", "freeMemoryBytes": 8192 * MIB}}, 4096 * MIB)
    assert args[1] == "RPC0,RPC1,MTL0" and args[-1] == "4928,4928,7952"


@pytest.mark.parametrize("memory", [None, True, "4096", -1, 2200 * MIB])
def test_missing_or_insufficient_memory_fails_closed(memory):
    with pytest.raises(SplitError):
        fit_arguments("RPC0: 127.0.0.1:1234 (10922 MiB, 10922 MiB free)",
                      {"1234": {"backend": "metal", "freeMemoryBytes": memory}}, 60000 * MIB)


@pytest.mark.parametrize("output", ["", "CUDA0: GPU (10000 MiB, 9000 MiB free)",
    "RPC0: 192.168.1.2:1234 (8000 MiB, 8000 MiB free)",
    "RPC0: 127.0.0.1:9999 (8000 MiB, 8000 MiB free)"])
def test_missing_or_unexpected_workers_cannot_be_silently_ignored(output):
    with pytest.raises(SplitError):
        fit_arguments(output, {"1234": {"backend": "cuda"}}, 60000 * MIB)


def test_cancelling_memory_probe_terminates_native_process(tmp_path):
    pidfile = tmp_path / "pid"
    executable = tmp_path / "llama-server"
    executable.write_text(f"#!{sys.executable}\nimport os,time,pathlib\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n")
    executable.chmod(0o700)
    async def run():
        task = asyncio.create_task(probe_fit_arguments(tmp_path, ["127.0.0.1:1234"], {}))
        try:
            async with asyncio.timeout(5):
                while not pidfile.exists():
                    await asyncio.sleep(.02)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    asyncio.run(run())


@pytest.mark.parametrize("available,expected", [
    (None, None), (True, None), ("4096", None), (-1, None),
    (1024 * MIB, {"availableMiB": 1024, "reserveMiB": 2048, "budgetMiB": 0}),
    (4096 * MIB + 17, {"availableMiB": 4096, "reserveMiB": 2048, "budgetMiB": 2048}),
])
def test_shared_memory_estimate_never_invents_capacity(available, expected):
    from potluck_local.services.split_memory import shared_memory_budget
    assert shared_memory_budget(available) == expected


# ---- L1 plan metadata checks (bench/scripts/split_equivalence.py l1) ----------
# No weights, binaries or GPUs. They run the planner's own functions over a
# matrix of synthetic device inventories and budgets and assert invariants.

import itertools
from pathlib import Path

from potluck_local.services.split_manager import head_arguments

BACKENDS = ("metal", "cpu", "cuda")


def _inventory(local: str, workers: list[tuple[int, int]]) -> str:
    """Synthetic --list-devices output: one local device then RPC workers."""
    lines = ["Available devices:", f"  {local}0: Local ({20000} MiB, {20000} MiB free)"]
    for i, (_, free) in enumerate(workers):
        lines.append(f"  RPC{i}: 127.0.0.1:{2000 + i} ({free + 500} MiB, {free} MiB free)")
    return "\n".join(lines) + "\n"


def _plan(local: str, backends: list[str], free: list[int], avail: list[int], local_avail=64000):
    workers = {str(2000 + i): {"backend": b, "freeMemoryBytes": a * MIB}
               for i, (b, a) in enumerate(zip(backends, avail))}
    return fit_arguments(_inventory(local, [(0, f) for f in free]), workers, local_avail * MIB)


MATRIX = [(local, backends, free, avail)
          for local in ("CUDA", "MTL")
          for n in (1, 2, 3)
          for backends in itertools.product(BACKENDS, repeat=n)
          for free in ([8000] * n, [4000, 16000, 24000][:n])
          for avail in ([6000] * n, [12000, 4096, 40000][:n])]


@pytest.mark.parametrize("local,backends,free,avail", MATRIX)
def test_fit_plan_invariants_over_device_matrix(local, backends, free, avail):
    args = _plan(local, backends, free, avail)
    assert args == _plan(local, backends, free, avail)  # deterministic
    assert args[0] == "--device" and args[2:4] == ["--fit", "on"] and args[4] == "--fit-target"
    devices, margins = args[1].split(","), [int(m) for m in args[5].split(",")]
    # One margin per device: workers in the order selected, then the head's device last.
    assert devices == [f"RPC{i}" for i in range(len(free))] + [f"{local}0"]
    assert len(margins) == len(devices) and all(m >= 1024 for m in margins)
    assert not set(args) & {"--n-gpu-layers", "-ngl", "--tensor-split", "-ts"}  # the engine decides
    usable = [f - m for f, m in zip(free, margins)] + [20000 - margins[-1]]
    for i, backend in enumerate(backends):
        if backend == "cuda":
            assert usable[i] == free[i] - 1024  # VRAM: native free minus 1 GiB
        else:  # shared RAM: a device never plans past the machine's lease budget
            assert usable[i] <= max(0, avail[i] - 2048)


@pytest.mark.parametrize("local,backends,free,avail", MATRIX)
def test_output_layer_lands_on_the_local_device(local, backends, free, avail):
    from potluck_local.services.split_layout import predict_layer_assignment
    devices = _plan(local, backends, free, avail)[1].split(",")
    assert devices[-1] == f"{local}0"
    for n in (24, 28, 36, 64, 80):
        for split in ([1] * len(devices), [free_ + 1 for free_ in free] + [20000], [1] * (len(devices) - 1) + [3]):
            assigned = predict_layer_assignment(n, [float(x) for x in split])
            assert devices[assigned[n]] == f"{local}0"


def test_no_local_device_keeps_worker_order():
    output = "RPC0: 127.0.0.1:1 (8000 MiB, 8000 MiB free)\nRPC1: 127.0.0.1:2 (8000 MiB, 8000 MiB free)"
    args = fit_arguments(output, {"1": {"backend": "cuda"}, "2": {"backend": "cuda"}}, 0)
    assert args[1] == "RPC0,RPC1"


def test_fit_plan_refuses_when_no_worker_has_budget():
    for backend in ("metal", "cpu"):
        with pytest.raises(SplitError, match="too little available memory"):
            _plan("CUDA", [backend], [8000], [2048 + 255])
    assert _plan("CUDA", ["metal"], [8000], [2048 + 256])  # exactly the minimum budget still plans


def test_head_command_line_is_loopback_layer_split_with_matching_endpoints(tmp_path):
    endpoints = ["127.0.0.1:41000", "127.0.0.1:41001"]
    memory = _plan("CUDA", ["cuda", "cuda"], [8000, 8000], [0, 0])
    argv = head_arguments(tmp_path, Path("m.gguf"), 41100, 8192, 4, endpoints, memory)
    assert argv == head_arguments(tmp_path, Path("m.gguf"), 41100, 8192, 4, endpoints, memory)
    assert argv[argv.index("--split-mode") + 1] == "layer" and argv[argv.index("--parallel") + 1] == "1"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--ctx-size") + 1] == "8192"
    assert argv[argv.index("--rpc") + 1].split(",") == endpoints
    assert len(argv[argv.index("--device") + 1].split(",")) == len(endpoints) + 1  # local + each worker
    assert argv[argv.index("--device") + 1].split(",")[-1] == "CUDA0"  # the head's device is last
    assert all(a.startswith("127.0.0.1:") for a in argv[argv.index("--rpc") + 1].split(","))
    assert "0.0.0.0" not in argv and "--n-gpu-layers" not in argv
