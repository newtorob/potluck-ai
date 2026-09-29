import asyncio
import os
import sys

import pytest

from potluck_local.services.split_memory import fit_arguments, probe_fit_arguments, MIB
from potluck_local.services.split_native import SplitError


def test_mac_budget_uses_available_ram_instead_of_metal_capacity():
    output = "Available devices:\n  CUDA0: NVIDIA (10811 MiB, 10500 MiB free)\n  RPC0: 127.0.0.1:1234 (10922 MiB, 10922 MiB free)\n"
    args = fit_arguments(output, {"1234": {"backend": "metal", "freeMemoryBytes": 4096 * MIB}}, 60000 * MIB)
    assert args == ["--device", "CUDA0,RPC0", "--fit", "on", "--fit-target", "1024,8874"]
    assert "--n-gpu-layers" not in args  # permit CPU overflow while preserving context


def test_shared_budget_is_divided_across_devices_on_same_machine():
    output = "MTL0: Apple (10000 MiB, 10000 MiB free)\nRPC0: 127.0.0.1:1234 (8000 MiB, 8000 MiB free)\nRPC1: 127.0.0.1:1234 (8000 MiB, 8000 MiB free)"
    args = fit_arguments(output, {"1234": {"backend": "cpu", "freeMemoryBytes": 8192 * MIB}}, 4096 * MIB)
    assert args[-1] == "7952,4928,4928"


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
