"""Linux GPU availability must come from the engine, not the PCI inventory."""
import sys
from types import SimpleNamespace

import pytest

from potluck_local.services import hardware_detect, llama_backend
from potluck_local.services.runtime import LLMRuntime

GIB = 1024 ** 3
NVIDIA = {"name": "Vulkan0", "description": "NVIDIA", "type": "gpu", "free_bytes": 4 * GIB, "total_bytes": 11 * GIB}
INTEL = {"name": "Vulkan1", "description": "Intel", "type": "igpu", "free_bytes": 40 * GIB, "total_bytes": 48 * GIB}


@pytest.fixture
def linux_host(monkeypatch):
    import psutil

    monkeypatch.setattr(hardware_detect.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware_detect.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=8 * GIB, available=5 * GIB))
    monkeypatch.setitem(sys.modules, "GPUtil", SimpleNamespace(getGPUs=lambda: [SimpleNamespace(name="Physical GPU", memoryTotal=32768)]))


def test_cpu_fallback_preserves_hardware_but_uses_ram_for_tier(linux_host, monkeypatch):
    monkeypatch.setattr(llama_backend, "current_selection", lambda: llama_backend.Selection("cpu", None, "driver unavailable", probed=True))
    info = hardware_detect.detect_hardware()
    assert info["gpu_name"] == "Physical GPU"
    assert info["gpu_vram_gb"] == 32
    assert info["inference_gpu_available"] is False
    assert info["inference_gpu_vram_gb"] is None
    assert info["recommended_tier"] == "small"


def test_backend_capacity_matches_selected_dedicated_gpu(linux_host, monkeypatch):
    monkeypatch.setattr(llama_backend, "current_selection", lambda: llama_backend.Selection("vulkan", None, "available", [NVIDIA, INTEL], True))
    info = hardware_detect.detect_hardware()
    assert info["inference_gpu_available"] is True
    assert info["inference_gpu_vram_gb"] == 11
    assert info["recommended_tier"] == "medium"


def test_offload_chooses_dedicated_before_shared_memory():
    assert llama_backend.offload_device([INTEL, NVIDIA]) == (1, NVIDIA)
    assert llama_backend.offload_device([INTEL]) == (0, INTEL)


@pytest.mark.parametrize("backend,requested,expected", [("cpu", 99, 0), ("vulkan", -1, -1)])
def test_runtime_applies_the_reported_device_and_cpu_fallback(tmp_path, monkeypatch, backend, requested, expected):
    import potluck_local.services.runtime as runtime_module

    calls = []

    class Model:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Model, LLAMA_SPLIT_MODE_NONE=0))
    monkeypatch.setattr(runtime_module, "_install_log_capture", lambda: None)
    monkeypatch.setattr(llama_backend, "current_selection", lambda: llama_backend.Selection(backend, None, "test", [NVIDIA, INTEL], True))
    monkeypatch.setattr(llama_backend, "describe_devices", lambda: [NVIDIA, INTEL])
    model = tmp_path / "test.gguf"
    model.write_bytes(b"small fixture")
    runtime = LLMRuntime()
    try:
        runtime.load_model(str(model), n_gpu_layers=requested, n_ctx=8192)
        assert calls[0]["n_gpu_layers"] == expected
        assert runtime._load_report["requested_n_gpu_layers"] == requested
        assert runtime._load_report["used_n_gpu_layers"] == expected
        if backend == "vulkan":
            assert calls[0]["main_gpu"] == 0
            assert calls[0]["split_mode"] == 0
            assert runtime._load_report["offload_device"]["description"] == "NVIDIA"
            assert runtime._load_report["offload_plan"]["free_vram_bytes"] == 4 * GIB
    finally:
        runtime.unload_model()
