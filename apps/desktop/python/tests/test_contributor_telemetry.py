"""Capacity and hardware facts the contributor reports."""
from __future__ import annotations

import types

from potluck_local.contributor import telemetry


def test_probe_capacity_never_raises_without_gpu(monkeypatch) -> None:
    class _NoGPUs:
        @staticmethod
        def getGPUs():
            raise RuntimeError("nvidia-smi missing")

    monkeypatch.setitem(__import__("sys").modules, "GPUtil", _NoGPUs)
    monkeypatch.setattr(telemetry._platform, "system", lambda: "Linux")
    cap = telemetry.probe_capacity()
    assert cap.gpu_utilization is None and cap.vram_available_gb is None


def test_probe_capacity_reads_nvidia_load_and_free_vram(monkeypatch) -> None:
    fake_gpu = types.SimpleNamespace(load=0.37, memoryFree=5632.0)

    class _GPUtil:
        @staticmethod
        def getGPUs():
            return [fake_gpu]

    monkeypatch.setitem(__import__("sys").modules, "GPUtil", _GPUtil)
    cap = telemetry.probe_capacity()
    assert cap.gpu_utilization == 0.37
    assert cap.vram_available_gb == 5.5


def test_probe_capacity_clamps_utilization(monkeypatch) -> None:
    fake_gpu = types.SimpleNamespace(load=1.7, memoryFree=1024.0)

    class _GPUtil:
        @staticmethod
        def getGPUs():
            return [fake_gpu]

    monkeypatch.setitem(__import__("sys").modules, "GPUtil", _GPUtil)
    assert telemetry.probe_capacity().gpu_utilization == 1.0


def test_probe_capacity_apple_silicon_uses_free_unified_memory(monkeypatch) -> None:
    class _GPUtil:
        @staticmethod
        def getGPUs():
            return []

    monkeypatch.setitem(__import__("sys").modules, "GPUtil", _GPUtil)
    monkeypatch.setattr(telemetry._platform, "system", lambda: "Darwin")
    monkeypatch.setattr(telemetry._platform, "machine", lambda: "arm64")
    fake_psutil = types.SimpleNamespace(
        virtual_memory=lambda: types.SimpleNamespace(available=6 * 1024 ** 3)
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
    cap = telemetry.probe_capacity()
    assert cap.gpu_utilization is None
    assert cap.vram_available_gb == 6.0


def test_full_hardware_summary_merges_detected_over_legacy() -> None:
    detected = {
        "os": "Darwin",
        "arch": "arm64",
        "cpu_name": "Apple M2 Pro",  # must not be forwarded
        "cpu_cores_physical": 10,
        "cpu_cores_logical": 10,
        "ram_total_gb": 16.0,
        "ram_available_gb": 3.2,  # dynamic, must not be forwarded
        "gpu_name": "Apple M2 Pro",
        "gpu_vram_gb": 16.0,
        "recommended_tier": "medium",
    }
    summary = telemetry.full_hardware_summary(detected)
    for legacy in ("system", "release", "machine", "cpu_count"):
        assert legacy in summary
    assert summary["gpu_name"] == "Apple M2 Pro"
    assert summary["gpu_vram_gb"] == 16.0
    assert summary["recommended_tier"] == "medium"
    assert summary["cpu_cores_physical"] == 10
    assert "cpu_name" not in summary
    assert "ram_available_gb" not in summary


def test_full_hardware_summary_skips_none_values_and_survives_detect_failure(monkeypatch) -> None:
    summary = telemetry.full_hardware_summary({"gpu_name": None, "gpu_vram_gb": None})
    assert "gpu_name" not in summary and "system" in summary

    import potluck_local.services.hardware_detect as hd

    def _boom():
        raise RuntimeError("no tools")

    monkeypatch.setattr(hd, "detect_hardware", _boom)
    summary = telemetry.full_hardware_summary(None)
    assert set(summary) == {"system", "release", "machine", "cpu_count"}
