"""What the app is allowed to claim about where a model is running.

`execution_verified` is the field a companion app trusts, so it must only be
true on evidence llama.cpp produced for this exact load. A detected graphics
card, a backend that supports offload, and a requested layer count are all
things 0.1.3 had while running entirely on the CPU.
"""

from __future__ import annotations

from potluck_local.services.runtime import LLMRuntime, _parse_load_log

VULKAN_LOG = [
    "load_tensors: offloading 32 repeating layers to GPU\n",
    "load_tensors: offloading output layer to GPU\n",
    "load_tensors: offloaded 33/33 layers to GPU\n",
    "load_tensors:      Vulkan0 model buffer size =  5115.49 MiB\n",
    "load_tensors:   CPU_Mapped model buffer size =   344.44 MiB\n",
    "llama_kv_cache_unified:    Vulkan0 KV buffer size =   256.00 MiB\n",
    "llama_context:      Vulkan0 compute buffer size =   258.50 MiB\n",
    "llama_context:  Vulkan_Host compute buffer size =    12.01 MiB\n",
]

CPU_LOG = [
    "load_tensors:   CPU_Mapped model buffer size =  5459.93 MiB\n",
]

PARTIAL_LOG = [
    "load_tensors: offloaded 18/33 layers to GPU\n",
    "load_tensors:      Vulkan0 model buffer size =  2800.00 MiB\n",
    "load_tensors:   CPU_Mapped model buffer size =  2660.00 MiB\n",
]


def _runtime(report: dict | None) -> LLMRuntime:
    runtime = LLMRuntime()
    runtime._model = object()  # stand-in for a loaded Llama handle
    runtime._model_id = "qwen3-4b-instruct-2507-q4"
    runtime._load_report = report
    return runtime


def _report(lines: list[str], devices: list[dict] | None = None) -> dict:
    return {
        "backend_selection": {"backend": "vulkan", "devices": devices or []},
        "requested_n_gpu_layers": -1,
        "used_n_gpu_layers": -1,
        "offload_plan": None,
        **_parse_load_log(lines),
    }


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def test_parses_offloaded_layers_and_buffers():
    parsed = _parse_load_log(VULKAN_LOG)
    assert parsed["offloaded_layers"] == 33
    assert parsed["total_layers"] == 33
    assert parsed["model_buffer_mib"] == {"Vulkan0": 5115.49, "CPU_Mapped": 344.44}


def test_parses_kv_and_compute_buffers():
    """A long context can put more on the card than the weights do, which is
    how a model that "fits" still spills. Report it separately."""
    parsed = _parse_load_log(VULKAN_LOG)
    assert parsed["kv_buffer_mib"] == {"Vulkan0": 256.00}
    assert parsed["compute_buffer_mib"] == {"Vulkan0": 258.50, "Vulkan_Host": 12.01}


def test_kv_and_compute_buffers_do_not_change_the_device_verdict():
    """Only the weight buffers decide where the model ran."""
    devices = [{"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"}]
    summary = _runtime(_report(VULKAN_LOG, devices)).inference_summary()
    assert summary["device_kind"] == "gpu"


def test_a_cpu_load_reports_no_offload_line():
    parsed = _parse_load_log(CPU_LOG)
    assert parsed["offloaded_layers"] is None
    assert parsed["model_buffer_mib"] == {"CPU_Mapped": 5459.93}


def test_unrelated_log_lines_are_ignored():
    parsed = _parse_load_log(["llama_model_loader: loaded meta data\n", "print_info: n_ctx = 4096\n"])
    assert parsed == {
        "offloaded_layers": None,
        "total_layers": None,
        "model_buffer_mib": None,
        "kv_buffer_mib": None,
        "compute_buffer_mib": None,
    }


# ---------------------------------------------------------------------------
# The summary a companion app reads
# ---------------------------------------------------------------------------


def test_no_model_means_no_claim():
    runtime = LLMRuntime()
    assert runtime.inference_summary() is None
    assert runtime.inference_details() is None


def test_a_load_with_no_evidence_is_not_verified():
    """The engine told us nothing, so we say nothing."""
    summary = _runtime(_report([])).inference_summary()
    assert summary["execution_verified"] is False
    assert summary["device_kind"] == "unknown"


def test_a_fully_offloaded_model_reports_the_gpu_it_ran_on():
    devices = [{"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"}]
    summary = _runtime(_report(VULKAN_LOG, devices)).inference_summary()
    assert summary["execution_verified"] is True
    assert summary["device_kind"] == "gpu"
    assert summary["backend"] == "vulkan"
    assert summary["device_name"] == "AMD Radeon RX 6600 XT"
    assert summary["model_id"] == "qwen3-4b-instruct-2507-q4"
    assert summary["schema_version"] == 1


def test_a_cpu_load_says_cpu_even_on_a_gpu_build():
    devices = [
        {"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"},
        {"name": "CPU", "description": "AMD Ryzen 7 5800X", "type": "cpu"},
    ]
    summary = _runtime(_report(CPU_LOG, devices)).inference_summary()
    assert summary["execution_verified"] is True
    assert summary["device_kind"] == "cpu"
    assert summary["backend"] == "cpu"
    assert summary["device_name"] == "AMD Ryzen 7 5800X"


def test_a_partial_offload_is_reported_as_mixed():
    devices = [{"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"}]
    summary = _runtime(_report(PARTIAL_LOG, devices)).inference_summary()
    assert summary["device_kind"] == "mixed"
    assert summary["backend"] == "vulkan"


def test_pinned_host_buffers_are_not_mistaken_for_device_memory():
    """CUDA_Host is system memory the GPU can reach, not weights on the card."""
    log = [
        "load_tensors:    CUDA_Host model buffer size =  4000.00 MiB\n",
        "load_tensors:   CPU_Mapped model buffer size =   344.44 MiB\n",
    ]
    summary = _runtime(_report(log)).inference_summary()
    assert summary["device_kind"] == "cpu"
    assert summary["backend"] == "cpu"


def test_the_backend_label_comes_from_the_device_that_holds_the_weights():
    log = [
        "load_tensors: offloaded 33/33 layers to GPU\n",
        "load_tensors:        CUDA0 model buffer size =  5115.49 MiB\n",
    ]
    summary = _runtime(_report(log)).inference_summary()
    assert summary["backend"] == "cuda"


def test_details_carry_the_summary_and_the_plan():
    devices = [{"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"}]
    report = _report(VULKAN_LOG, devices)
    report["offload_plan"] = {"n_gpu_layers": -1, "reason": "the whole model fits in free VRAM"}
    details = _runtime(report).inference_details()
    assert details["summary"]["device_kind"] == "gpu"
    assert details["offload_plan"]["reason"] == "the whole model fits in free VRAM"
    assert details["requested_n_gpu_layers"] == -1


def test_the_summary_carries_no_paths_or_prompts():
    """Health is readable by a companion app; it must not leak local detail."""
    devices = [{"name": "Vulkan0", "description": "AMD Radeon RX 6600 XT", "type": "gpu"}]
    runtime = _runtime(_report(VULKAN_LOG, devices))
    runtime._model_path = "/Users/someone/.potluck/models/thing/model.gguf"
    summary = runtime.inference_summary()
    assert "model_path" not in summary
    assert not any("/" in str(v) for v in summary.values() if isinstance(v, str))
