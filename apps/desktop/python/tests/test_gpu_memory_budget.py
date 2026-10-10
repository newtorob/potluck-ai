"""The offload plan against what the rest of the machine is using (Windows).

The measured case is an RX 6600 XT with 8 GB, qwen3-8b-q4 (a 5,027,783,488 byte
GGUF, 36 blocks), a game and the desktop holding about 1.9 GiB of the card, and
the Vulkan probe reporting 7,712,657,152 bytes free of 8,573,157,376. The plan
filled the card, every inference then ran out of device memory, and nothing said
why. The Windows reader is replaced throughout: these tests run on any platform.
"""

from __future__ import annotations

import logging
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from potluck_local.services import llama_backend, windows_gpu
from potluck_local.services.windows_gpu import AdapterMemory, AdapterReading

GIB = 1024 ** 3

# Recorded on the machine above, 2026-10-09 (previous display driver).
MODEL_BYTES = 5_027_783_488
BLOCKS, KV_HEADS, HEAD_DIM = 36, 8, 128
KV_PER_TOKEN = BLOCKS * KV_HEADS * (HEAD_DIM + HEAD_DIM) * 2  # 147,456, as llama.cpp logs it
GGML_FREE = 7_712_657_152
GGML_TOTAL = 8_573_157_376  # Vulkan probe and registry qwMemorySize agree
DXGI_TOTAL = 8_543_391_744  # DXGI DedicatedVideoMemory for the same card
RX = "AMD Radeon RX 6600 XT"
LUID = "0x00000000_0x00009335"
OTHERS = int(1.9 * GIB)  # game 1.45 + compositor 0.33 + app window 0.13 + browser 0.08, rounded
ENGINE_MEASURED = int(5.92 * GIB)  # the engine's own dedicated use after the failed load


def _str(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_model(path: Path, size: int = MODEL_BYTES, blocks: int = BLOCKS) -> Path:
    """A GGUF header complete enough to size a load, sparse-padded to *size* bytes."""
    kvs = [
        ("general.architecture", 8, _str("qwen3")),
        ("qwen3.block_count", 4, struct.pack("<I", blocks)),
        ("qwen3.attention.head_count_kv", 4, struct.pack("<I", KV_HEADS)),
        ("qwen3.attention.key_length", 4, struct.pack("<I", HEAD_DIM)),
        ("qwen3.attention.value_length", 4, struct.pack("<I", HEAD_DIM)),
    ]
    with open(path, "wb") as fh:
        fh.write(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs)))
        for key, vtype, value in kvs:
            raw = key.encode()
            fh.write(struct.pack("<Q", len(raw)) + raw + struct.pack("<I", vtype) + value)
        fh.seek(size - 1)
        fh.write(b"\0")
    return path


@pytest.fixture
def model(tmp_path) -> Path:
    return write_model(tmp_path / "model.gguf")


def card(free: int = GGML_FREE, total: int = GGML_TOTAL, description: str = RX, kind: str = "gpu", name: str = "Vulkan0") -> dict:
    return {"name": name, "description": description, "type": kind, "free_bytes": free, "total_bytes": total}


def adapter(used: int, *, own: int = 0, description: str | None = RX, total: int | None = DXGI_TOTAL,
            luid: str = LUID, software: bool = False) -> AdapterMemory:
    return AdapterMemory(luid, description, total, used, own, software)


def windows_reads(monkeypatch, *adapters: AdapterMemory, source: str = "pdh+dxgi"):
    monkeypatch.setattr(
        llama_backend, "read_system_gpu_memory", lambda: AdapterReading("ok", tuple(adapters), source=source)
    )


def plan(model, devices, *, n_ctx=32768, requested=-1):
    return llama_backend.plan_gpu_layers(model, requested=requested, n_ctx=n_ctx, devices=devices)


def today(model, devices, monkeypatch, **kw):
    """The plan the code made before the system-wide figure existed: reader says 'not Windows'."""
    monkeypatch.setattr(
        llama_backend, "read_system_gpu_memory", lambda: AdapterReading("not_applicable", detail="not Windows")
    )
    return plan(model, devices, **kw)


# ---------------------------------------------------------------------------
# The measured case
# ---------------------------------------------------------------------------


def test_the_driver_figure_alone_fills_the_card_as_it_did_on_the_machine(model, monkeypatch):
    p = today(model, [card()], monkeypatch)
    assert (p.n_gpu_layers, p.n_ctx) == (-1, 8192)
    assert p.reason == "context reduced from 32768 to 8192 so the KV cache fits in VRAM"


def test_other_programs_holding_the_card_make_the_plan_offload_fewer_layers(model, monkeypatch):
    windows_reads(monkeypatch, adapter(OTHERS))
    p = plan(model, [card()])
    # Budget: min(7,712,657,152, 8,543,391,744 - 2,040,109,465) - 1 GiB = 5,429,540,455 bytes.
    # One layer at 8192 tokens costs (5,027,783,488 + 147,456 * 8192) / 36 = 173,214,528 bytes.
    assert p.budget_bytes == DXGI_TOTAL - OTHERS - 1 * GIB
    assert p.n_ctx == 8192
    assert p.n_gpu_layers == 31
    assert p.total_layers == 36
    assert "31 of 36 layers fit at an 8192-token context" in p.reason
    assert "1.9 GB of the card is in use by other programs" in p.reason


def test_the_planned_load_leaves_room_to_run_beside_the_other_programs(model, monkeypatch):
    """Plan at 31 layers, add what the engine really allocates beyond weights and KV
    cache, add the other programs, and the card still has the sharing margin free."""
    windows_reads(monkeypatch, adapter(OTHERS))
    p = plan(model, [card()])
    weights_plus_kv = MODEL_BYTES + KV_PER_TOKEN * p.n_ctx
    share_on_card = weights_plus_kv * p.n_gpu_layers / p.total_layers
    engine_use = share_on_card + llama_backend._ENGINE_OVERHEAD_BYTES
    assert DXGI_TOTAL - (engine_use + OTHERS) >= llama_backend._SHARING_MARGIN_BYTES


def test_the_old_full_offload_would_not_have_left_that_room(model):
    """The engine measured 5.92 GiB for the full offload, with 1.9 GiB held elsewhere."""
    spare = DXGI_TOTAL - (ENGINE_MEASURED + OTHERS)
    assert spare < llama_backend._SHARING_MARGIN_BYTES
    assert spare < 0.2 * GIB


def test_weights_plus_kv_understates_the_measured_use_by_less_than_the_engine_overhead():
    """qwen3-8b-q4 at 8192 tokens: 5.92 GiB measured on the card, 0.11 GiB over the two terms."""
    weights_plus_kv = MODEL_BYTES + KV_PER_TOKEN * 8192
    shortfall = ENGINE_MEASURED - weights_plus_kv
    assert 0 < shortfall < 0.15 * GIB
    assert shortfall <= llama_backend._ENGINE_OVERHEAD_BYTES
    assert llama_backend._VRAM_RESERVE_BYTES == 1 * GIB  # unchanged: the parts are inside it
    assert (
        llama_backend._ENGINE_OVERHEAD_BYTES + llama_backend._SHARING_MARGIN_BYTES
        == llama_backend._VRAM_RESERVE_BYTES
    )


def test_the_plan_reports_what_it_used_and_why(model, monkeypatch):
    windows_reads(monkeypatch, adapter(OTHERS))
    p = plan(model, [card()])
    assert p.reported_free_vram_bytes == GGML_FREE
    assert p.free_vram_bytes == DXGI_TOTAL - OTHERS
    assert p.other_programs_vram_bytes == OTHERS
    assert p.vram_total_bytes == DXGI_TOTAL
    assert p.reserve_bytes == 1 * GIB
    use = p.system_use
    assert (use["status"], use["matched_by"], use["adapter"], use["source"]) == ("ok", "description", RX, "pdh+dxgi")
    assert "other programs are using 1.9 GB" in p.memory_note
    assert "the driver reported 7.2 GB free" in p.memory_note
    assert p.as_dict()["system_use"]["luid"] == LUID


# ---------------------------------------------------------------------------
# Unchanged where nothing changes
# ---------------------------------------------------------------------------


def test_an_idle_card_gets_the_plan_it_always_got(model, monkeypatch):
    before = today(model, [card()], monkeypatch)
    windows_reads(monkeypatch, adapter(int(0.3 * GIB)))  # the compositor and a window
    after = plan(model, [card()])
    assert (after.n_gpu_layers, after.n_ctx, after.reason) == (before.n_gpu_layers, before.n_ctx, before.reason)
    assert after.free_vram_bytes == before.free_vram_bytes == GGML_FREE
    assert "other programs" not in after.reason
    assert after.system_use["status"] == "ok"  # still reported, it just did not matter


def test_this_engines_own_leftover_use_is_not_counted_against_the_new_load(model, monkeypatch):
    """A model still being released shows up in the adapter counter. It is not another program."""
    windows_reads(monkeypatch, adapter(OTHERS + 4 * GIB, own=4 * GIB))
    p = plan(model, [card()])
    assert p.other_programs_vram_bytes == OTHERS
    assert p.system_use["own_bytes"] == 4 * GIB
    assert p.n_gpu_layers == 31  # the same plan as when only the other programs are on the card


def test_a_pin_is_honoured_and_the_reader_is_not_consulted(model, monkeypatch):
    monkeypatch.setattr(
        llama_backend, "read_system_gpu_memory", lambda: pytest.fail("a pinned plan must not read the card")
    )
    p = plan(model, [card()], requested=20)
    assert p.n_gpu_layers == 20 and p.system_use is None


def test_other_platforms_see_no_system_figure_and_no_note(model, monkeypatch):
    p = today(model, [card()], monkeypatch)
    assert p.system_use is None and p.memory_note is None
    assert p.other_programs_vram_bytes is None and p.reported_free_vram_bytes == GGML_FREE


def test_the_real_reader_says_not_applicable_off_windows():
    if sys.platform == "win32":
        pytest.skip("the real reader runs on Windows")
    reading = windows_gpu.read_adapter_memory()
    assert reading.status == "not_applicable" and reading.adapters == ()


# ---------------------------------------------------------------------------
# The reader cannot be read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reading", [
    AdapterReading("unavailable", detail="this Windows has no GPU memory performance counters"),
    AdapterReading("ok", (), source="pdh"),  # counters present, no adapter listed
])
def test_an_unreadable_figure_leaves_the_plan_as_it_was_and_says_so(model, monkeypatch, reading):
    before = today(model, [card()], monkeypatch)
    monkeypatch.setattr(llama_backend, "read_system_gpu_memory", lambda: reading)
    after = plan(model, [card()])
    assert (after.n_gpu_layers, after.n_ctx, after.reason) == (before.n_gpu_layers, before.n_ctx, before.reason)
    assert after.free_vram_bytes == GGML_FREE
    assert after.system_use["status"] == "unavailable"
    assert "unavailable" in after.memory_note
    assert "driver's free-memory figure" in after.memory_note


def test_a_reader_that_raises_cannot_stop_a_load(model, monkeypatch):
    def boom():
        raise OSError("pdh went away")

    monkeypatch.setattr(llama_backend, "read_system_gpu_memory", boom)
    p = plan(model, [card()])
    assert (p.n_gpu_layers, p.n_ctx) == (-1, 8192)
    assert p.system_use["status"] == "unavailable" and "OSError" in p.system_use["detail"]


# ---------------------------------------------------------------------------
# Order of preference: full, smaller context, fewer layers, CPU
# ---------------------------------------------------------------------------


PARTIAL = "some layers"


@pytest.mark.parametrize("others_gib,layers,ctx,words", [
    (0.0, -1, 32768, "model and its KV cache fit"),
    (6.0, -1, 28672, "context reduced from 32768 to 28672"),
    (9.0, -1, 8192, "context reduced from 32768 to 8192"),
    (10.5, PARTIAL, 8192, "layers fit at an 8192-token context"),
    (15.9, 0, 32768, "runs on the CPU"),
])
def test_the_planner_gives_up_context_before_layers_and_layers_before_the_gpu(
    model, monkeypatch, others_gib, layers, ctx, words
):
    """A 16 GiB card with 15 GiB free by the driver's account and a growing amount used elsewhere."""
    big = card(free=15 * GIB, total=16 * GIB)
    windows_reads(monkeypatch, adapter(int(others_gib * GIB), total=16 * GIB))
    p = plan(model, [big])
    assert words in p.reason
    assert p.n_ctx == ctx
    if layers == PARTIAL:
        assert 0 < p.n_gpu_layers < p.total_layers
    else:
        assert p.n_gpu_layers == layers
    if others_gib:
        assert "in use by other programs" in p.reason
    assert p.n_ctx >= llama_backend._MIN_GPU_CTX


def test_a_card_the_other_programs_have_filled_falls_back_to_the_cpu_and_says_why(model, monkeypatch):
    windows_reads(monkeypatch, adapter(DXGI_TOTAL - GIB // 2))
    p = plan(model, [card()])
    assert p.n_gpu_layers == 0
    assert "runs on the CPU" in p.reason and "in use by other programs" in p.reason


def test_more_in_use_than_the_card_has_is_clamped_not_negative(model, monkeypatch):
    windows_reads(monkeypatch, adapter(DXGI_TOTAL + GIB))
    p = plan(model, [card()])
    assert p.free_vram_bytes == 0 and p.n_gpu_layers == 0


# ---------------------------------------------------------------------------
# Which adapter is the one being offloaded to
# ---------------------------------------------------------------------------

INTEL = "Intel(R) UHD Graphics 770"


def intel(used=int(0.2 * GIB)) -> AdapterMemory:
    return adapter(used, description=INTEL, total=128 * 1024 ** 2, luid="0x00000000_0x00001111")


def test_two_adapters_are_matched_by_name(model, monkeypatch):
    windows_reads(monkeypatch, intel(), adapter(OTHERS))
    devices = [card(name="Vulkan0"), card(name="Vulkan1", description=INTEL, kind="igpu", free=24 * GIB, total=32 * GIB)]
    p = plan(model, devices)
    assert p.system_use["matched_by"] == "description" and p.system_use["luid"] == LUID
    assert p.other_programs_vram_bytes == OTHERS


def test_the_busy_integrated_adapter_is_not_mistaken_for_the_dedicated_one(model, monkeypatch):
    windows_reads(monkeypatch, adapter(int(0.3 * GIB)), intel(used=int(3 * GIB)))
    devices = [card(name="Vulkan0"), card(name="Vulkan1", description=INTEL, kind="igpu", free=24 * GIB, total=32 * GIB)]
    p = plan(model, devices)
    assert p.system_use["adapter"] == RX
    assert p.other_programs_vram_bytes == int(0.3 * GIB)


def test_a_differently_spelled_name_falls_back_to_the_dedicated_size(model, monkeypatch):
    windows_reads(monkeypatch, intel(), adapter(OTHERS, description="Radeon (TM) RX6600XT Graphics Adapter"))
    p = plan(model, [card(description="AMD Radeon RX 6600 XT (RADV NAVI23)")])
    assert p.system_use["matched_by"] == "dedicated_size"
    assert p.other_programs_vram_bytes == OTHERS


def test_when_neither_name_nor_size_agree_the_largest_dedicated_adapter_is_used(model, monkeypatch):
    windows_reads(monkeypatch, intel(), adapter(OTHERS, description="Some Other Name"))
    p = plan(model, [card(description="Unrelated", total=24 * GIB, free=20 * GIB)])
    assert p.system_use["matched_by"] == "largest_dedicated_total"
    assert p.system_use["luid"] == LUID


def test_two_identical_cards_assume_the_busier_one(model, monkeypatch):
    other = adapter(int(0.2 * GIB), luid="0x00000000_0x0000aaaa")
    windows_reads(monkeypatch, other, adapter(OTHERS))
    p = plan(model, [card(name="Vulkan0"), card(name="Vulkan1")])
    assert p.system_use["matched_by"] == "description_most_used"
    assert p.other_programs_vram_bytes == OTHERS


def test_the_software_adapter_is_ignored(model, monkeypatch):
    basic = adapter(0, description="Microsoft Basic Render Driver", total=0, luid="0x00000000_0x00009df6", software=True)
    windows_reads(monkeypatch, adapter(OTHERS), basic)
    p = plan(model, [card()])
    assert p.system_use["luid"] == LUID and p.system_use["matched_by"] == "description"


def test_one_listed_adapter_with_no_identity_is_the_only_adapter(model, monkeypatch):
    windows_reads(monkeypatch, adapter(OTHERS, description=None, total=None), source="pdh")
    p = plan(model, [card()])
    assert p.system_use["matched_by"] == "only_adapter" and p.system_use["source"] == "pdh"
    assert p.vram_total_bytes == GGML_TOTAL  # no DXGI size, so ggml's total
    assert p.other_programs_vram_bytes == OTHERS


def test_several_adapters_with_no_identity_cannot_be_told_apart(model, monkeypatch):
    windows_reads(
        monkeypatch,
        adapter(OTHERS, description=None, total=None),
        adapter(0, description=None, total=None, luid="0x00000000_0x00001111"),
        source="pdh",
    )
    before = today(model, [card()], monkeypatch)
    windows_reads(
        monkeypatch,
        adapter(OTHERS, description=None, total=None),
        adapter(0, description=None, total=None, luid="0x00000000_0x00001111"),
        source="pdh",
    )
    p = plan(model, [card()])
    assert p.system_use["status"] == "unavailable" and "which of several adapters" in p.system_use["detail"]
    assert (p.n_gpu_layers, p.n_ctx) == (before.n_gpu_layers, before.n_ctx)


def test_an_integrated_offload_device_is_not_sized_from_dedicated_counters(model, monkeypatch):
    windows_reads(monkeypatch, intel(used=int(0.1 * GIB)))
    igpu = card(description=INTEL, kind="igpu", free=24 * GIB, total=32 * GIB)
    p = plan(model, [igpu])
    assert p.system_use["status"] == "unavailable" and "integrated" in p.system_use["detail"]
    assert p.free_vram_bytes == 24 * GIB


# ---------------------------------------------------------------------------
# Windows reader pieces that do not need Windows
# ---------------------------------------------------------------------------


def as_windows(monkeypatch):
    monkeypatch.setattr(windows_gpu, "sys", SimpleNamespace(platform="win32"))


def test_pdh_rows_become_usage_by_adapter_and_this_process(monkeypatch):
    as_windows(monkeypatch)
    adapter_rows = [
        (f"luid_0x00000000_0x00009335_phys_0", 4_890_152_960),
        (f"luid_0x00000000_0x00009df6_phys_0", 0),
        ("not_an_adapter_instance", 5),
    ]
    process_rows = [
        ("pid_22144_luid_0x00000000_0x00009335_phys_0", 4_347_000_000),
        ("pid_8028_luid_0x00000000_0x00009335_phys_0", 388_000_000),
        ("pid_22144_luid_0x00000000_0x00009df6_phys_0", 0),
    ]
    results = iter([adapter_rows, process_rows])
    monkeypatch.setattr(windows_gpu, "_pdh_read", lambda paths: [next(results)])
    everyone, own = windows_gpu.pdh_dedicated_usage(pid=22144)
    assert everyone == {LUID: 4_890_152_960, "0x00000000_0x00009df6": 0}
    assert own == {LUID: 4_347_000_000, "0x00000000_0x00009df6": 0}


def test_a_failing_per_process_counter_only_means_nothing_is_subtracted(monkeypatch):
    as_windows(monkeypatch)
    calls = []

    def read(paths):
        calls.append(paths)
        if len(calls) == 2:
            raise windows_gpu.WindowsGpuUnavailable("per-process counters missing")
        return [[("luid_0x00000000_0x00009335_phys_0", 123)]]

    monkeypatch.setattr(windows_gpu, "_pdh_read", read)
    assert windows_gpu.pdh_dedicated_usage(pid=1) == ({LUID: 123}, {})


def test_read_adapter_memory_joins_pdh_to_dxgi_and_fails_soft(monkeypatch):
    as_windows(monkeypatch)
    monkeypatch.setattr(windows_gpu, "pdh_dedicated_usage", lambda: ({LUID: 100, "0x00000000_0x00009df6": 0}, {LUID: 40}))
    monkeypatch.setattr(windows_gpu, "dxgi_adapters", lambda: [
        {"luid": LUID, "description": RX, "dedicated_total_bytes": DXGI_TOTAL, "software": False},
        {"luid": "0x00000000_0x00009df6", "description": "Microsoft Basic Render Driver",
         "dedicated_total_bytes": 0, "software": True},
    ])
    reading = windows_gpu.read_adapter_memory()
    assert reading.status == "ok" and reading.source == "pdh+dxgi"
    rx = next(a for a in reading.adapters if a.luid == LUID)
    assert (rx.description, rx.dedicated_total_bytes, rx.dedicated_used_bytes, rx.own_used_bytes) == (RX, DXGI_TOTAL, 100, 40)

    def no_dxgi():
        raise windows_gpu.WindowsGpuUnavailable("dxgi.dll could not be loaded")

    monkeypatch.setattr(windows_gpu, "dxgi_adapters", no_dxgi)
    reading = windows_gpu.read_adapter_memory()
    assert reading.status == "ok" and reading.source == "pdh"
    assert all(a.description is None for a in reading.adapters)

    def no_pdh():
        raise windows_gpu.WindowsGpuUnavailable("this Windows has no GPU memory performance counters")

    monkeypatch.setattr(windows_gpu, "pdh_dedicated_usage", no_pdh)
    reading = windows_gpu.read_adapter_memory()
    assert reading.status == "unavailable" and "performance counters" in reading.detail


def test_adapter_names_match_across_vulkan_dxgi_and_wmi_spellings():
    assert windows_gpu.names_match("AMD Radeon RX 6600 XT", "amd radeon rx 6600 xt")
    assert windows_gpu.names_match("Intel(R) UHD Graphics 770", "Intel UHD Graphics 770")
    assert windows_gpu.names_match("Intel(R) Arc(TM) A770 Graphics", "Intel Arc A770 Graphics")
    assert not windows_gpu.names_match("AMD Radeon RX 6600 XT", "AMD Radeon RX 6600")  # another card
    assert not windows_gpu.names_match("NVIDIA GeForce RTX 2080 Ti", "NVIDIA GeForce RTX 2080")
    assert not windows_gpu.names_match(None, "AMD Radeon RX 6600 XT")


# ---------------------------------------------------------------------------
# The load report and the log line
# ---------------------------------------------------------------------------


def test_the_load_report_and_log_say_what_was_decided_and_why(tmp_path, monkeypatch, caplog):
    import potluck_local.services.runtime as runtime_module

    calls = []

    class Llama:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Llama, LLAMA_SPLIT_MODE_NONE=0))
    monkeypatch.setattr(runtime_module, "_install_log_capture", lambda: None)
    monkeypatch.setattr(runtime_module, "artifact_identity", lambda path: ("0" * 64, (1, 2, 3, 4)))
    monkeypatch.setattr(
        llama_backend, "current_selection", lambda: llama_backend.Selection("vulkan", None, "test", [card()], True)
    )
    monkeypatch.setattr(llama_backend, "describe_devices", lambda: [card()])
    windows_reads(monkeypatch, adapter(OTHERS))
    (tmp_path / "qwen3-8b-q4").mkdir()
    path = write_model(tmp_path / "qwen3-8b-q4" / "model.gguf")

    rt = runtime_module.LLMRuntime()
    try:
        with caplog.at_level(logging.INFO, logger=runtime_module.logger.name):
            rt.load_model(str(path), n_gpu_layers=-1, n_ctx=32768)
        report = rt._load_report["offload_plan"]
    finally:
        rt.unload_model()

    assert calls[0]["n_gpu_layers"] == 31 and calls[0]["n_ctx"] == 8192
    assert report["n_gpu_layers"] == 31 and report["other_programs_vram_bytes"] == OTHERS
    assert "1.9 GB of the card is in use by other programs" in report["reason"]
    assert "other programs are using 1.9 GB" in report["memory_note"]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "gpu_offload_planned requested_layers=-1 using_layers=31 requested_n_ctx=32768 using_n_ctx=8192" in text
    assert "31 of 36 layers fit at an 8192-token context, because 1.9 GB of the card is in use by other programs" in text
    assert "gpu_memory_check status=ok adapter=AMD Radeon RX 6600 XT matched_by=description source=pdh+dxgi" in text


def test_the_report_in_memory_carries_the_plan_and_the_unavailable_note(tmp_path, monkeypatch):
    import potluck_local.services.runtime as runtime_module

    class Llama:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=Llama, LLAMA_SPLIT_MODE_NONE=0))
    monkeypatch.setattr(runtime_module, "_install_log_capture", lambda: None)
    monkeypatch.setattr(runtime_module, "artifact_identity", lambda path: ("0" * 64, (1, 2, 3, 4)))
    monkeypatch.setattr(
        llama_backend, "current_selection", lambda: llama_backend.Selection("vulkan", None, "test", [card()], True)
    )
    monkeypatch.setattr(llama_backend, "describe_devices", lambda: [card()])
    monkeypatch.setattr(
        llama_backend, "read_system_gpu_memory", lambda: AdapterReading("unavailable", detail="no counters")
    )
    (tmp_path / "m").mkdir()
    path = write_model(tmp_path / "m" / "model.gguf")
    rt = runtime_module.LLMRuntime()
    try:
        rt.load_model(str(path), n_gpu_layers=-1, n_ctx=32768)
        plan_report = rt._load_report["offload_plan"]
        assert plan_report["system_use"]["status"] == "unavailable"
        assert plan_report["system_use"]["detail"] == "no counters"
        assert "unavailable" in plan_report["memory_note"]
        assert rt.inference_details()["offload_plan"]["memory_note"] == plan_report["memory_note"]
    finally:
        rt.unload_model()
