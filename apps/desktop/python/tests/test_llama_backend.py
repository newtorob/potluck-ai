"""Backend selection, GGUF layer reading, and offload planning.

These cover the decisions that made Windows 0.1.3 ship a CPU-only build and
then claim a GPU anyway: which backend set gets loaded, how many layers are
actually asked for, and what the app is allowed to say about it.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from potluck_local.services import llama_backend


@pytest.fixture(autouse=True)
def _clean_selection(monkeypatch, tmp_path):
    llama_backend.reset_for_tests()
    monkeypatch.delenv(llama_backend.BACKEND_ENV, raising=False)
    monkeypatch.delenv(llama_backend.LIB_PATH_ENV, raising=False)
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "potluck"))
    yield
    llama_backend.reset_for_tests()


# ---------------------------------------------------------------------------
# GGUF header
# ---------------------------------------------------------------------------


def _write_gguf(path: Path, kvs: list[tuple[str, int, bytes]]) -> None:
    """Minimal GGUF v3 header. kvs entries are (key, value_type, packed_value)."""
    with open(path, "wb") as fh:
        fh.write(b"GGUF")
        fh.write(struct.pack("<I", 3))
        fh.write(struct.pack("<Q", 0))  # tensor_count
        fh.write(struct.pack("<Q", len(kvs)))
        for key, vtype, value in kvs:
            encoded = key.encode("utf-8")
            fh.write(struct.pack("<Q", len(encoded)))
            fh.write(encoded)
            fh.write(struct.pack("<I", vtype))
            fh.write(value)


def _str_value(text: str) -> bytes:
    encoded = text.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def test_reads_block_count_past_other_keys(tmp_path):
    model = tmp_path / "model.gguf"
    _write_gguf(
        model,
        [
            ("general.architecture", 8, _str_value("llama")),
            ("general.name", 8, _str_value("test model")),
            ("llama.context_length", 4, struct.pack("<I", 8192)),
            ("llama.block_count", 4, struct.pack("<I", 32)),
        ],
    )
    assert llama_backend.read_gguf_layer_count(model) == 32


def test_reads_block_count_after_a_string_array(tmp_path):
    """Tokenizer vocabularies are string arrays and sit before block_count."""
    model = tmp_path / "model.gguf"
    array = struct.pack("<I", 8) + struct.pack("<Q", 3)
    array += _str_value("a") + _str_value("bb") + _str_value("ccc")
    _write_gguf(
        model,
        [
            ("tokenizer.ggml.tokens", 9, array),
            ("qwen3.block_count", 4, struct.pack("<I", 36)),
        ],
    )
    assert llama_backend.read_gguf_layer_count(model) == 36


def test_reads_block_count_after_a_numeric_array(tmp_path):
    model = tmp_path / "model.gguf"
    array = struct.pack("<I", 5) + struct.pack("<Q", 4) + struct.pack("<iiii", 1, 2, 3, 4)
    _write_gguf(
        model,
        [
            ("tokenizer.ggml.token_type", 9, array),
            ("llama.block_count", 10, struct.pack("<Q", 80)),
        ],
    )
    assert llama_backend.read_gguf_layer_count(model) == 80


def test_returns_none_for_a_file_that_is_not_gguf(tmp_path):
    junk = tmp_path / "not-a-model.bin"
    junk.write_bytes(b"\x00" * 512)
    assert llama_backend.read_gguf_layer_count(junk) is None


def test_returns_none_for_a_truncated_header(tmp_path):
    model = tmp_path / "truncated.gguf"
    model.write_bytes(b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0)[:4])
    assert llama_backend.read_gguf_layer_count(model) is None


def test_returns_none_when_the_file_is_missing(tmp_path):
    assert llama_backend.read_gguf_layer_count(tmp_path / "absent.gguf") is None


def test_returns_none_when_block_count_is_absent(tmp_path):
    model = tmp_path / "model.gguf"
    _write_gguf(model, [("general.architecture", 8, _str_value("llama"))])
    assert llama_backend.read_gguf_layer_count(model) is None


# ---------------------------------------------------------------------------
# Offload planning
# ---------------------------------------------------------------------------


def _gpu(free_gb: float, total_gb: float = 8.0) -> dict:
    return {
        "name": "Vulkan0",
        "description": "Test GPU",
        "type": "gpu",
        "free_bytes": int(free_gb * 1024**3),
        "total_bytes": int(total_gb * 1024**3),
    }


def _model_file(
    tmp_path: Path,
    gb: float,
    layers: int | None = 32,
    kv_heads: int | None = 8,
    head_dim: int = 128,
) -> Path:
    """A GGUF header just complete enough to size a load, padded to *gb*.

    kv_heads=None omits the attention metadata, which is the "KV cache cannot
    be sized from this header" case.
    """
    path = tmp_path / "model.gguf"
    if layers is None:
        path.write_bytes(b"NOTGGUF!")
    else:
        kvs = [("llama.block_count", 4, struct.pack("<I", layers))]
        if kv_heads is not None:
            kvs += [
                ("llama.attention.head_count_kv", 4, struct.pack("<I", kv_heads)),
                ("llama.attention.key_length", 4, struct.pack("<I", head_dim)),
                ("llama.attention.value_length", 4, struct.pack("<I", head_dim)),
            ]
        _write_gguf(path, kvs)
    with open(path, "r+b") as fh:
        fh.seek(int(gb * 1024**3) - 1)
        fh.write(b"\0")
    return path


def _plan(model, *, requested=-1, n_ctx=4096, devices=None):
    return llama_backend.plan_gpu_layers(
        model, requested=requested, n_ctx=n_ctx, devices=devices
    )


def test_a_pinned_layer_count_is_never_overridden(tmp_path):
    plan = _plan(_model_file(tmp_path, 0.001), requested=12, devices=[_gpu(0.1)])
    assert plan.n_gpu_layers == 12
    assert "pinned" in plan.reason


def test_no_gpu_device_means_no_offload(tmp_path):
    plan = _plan(
        _model_file(tmp_path, 0.001),
        devices=[{"name": "CPU", "type": "cpu", "free_bytes": 1, "total_bytes": 1}],
    )
    assert plan.n_gpu_layers == 0
    assert "no GPU device" in plan.reason


def test_a_model_that_fits_asks_for_every_layer(tmp_path):
    model = _model_file(tmp_path, 2.5, layers=36)
    plan = _plan(model, devices=[_gpu(7.5)])
    assert plan.n_gpu_layers == -1
    assert plan.n_ctx == 4096
    assert plan.free_vram_bytes == int(7.5 * 1024**3)


def test_a_model_that_does_not_fit_is_sized_down(tmp_path):
    # 6 GiB of weights, 4 GiB free: 3 GiB of budget after the reserve, so
    # roughly half the layers.
    model = _model_file(tmp_path, 6.0, layers=32, kv_heads=None)
    plan = _plan(model, devices=[_gpu(4.0)])
    assert 0 < plan.n_gpu_layers < 32
    assert plan.total_layers == 32
    assert "fit in free VRAM" in plan.reason


def test_a_card_with_no_room_falls_back_to_the_cpu(tmp_path):
    model = _model_file(tmp_path, 6.0, layers=32)
    plan = _plan(model, devices=[_gpu(0.5)])
    assert plan.n_gpu_layers == 0
    assert "runs on the CPU" in plan.reason


def test_an_oversized_model_with_no_layer_count_stays_on_the_cpu(tmp_path):
    """Better honest CPU than a guess that streams weights over PCIe per token."""
    model = _model_file(tmp_path, 6.0, layers=None)
    plan = _plan(model, devices=[_gpu(2.0)])
    assert plan.n_gpu_layers == 0
    assert "layer count is unreadable" in plan.reason


def test_the_plan_shrinks_with_the_reported_free_memory(tmp_path):
    """Whatever the backend reports as free is what the plan is sized against."""
    model = _model_file(tmp_path, 5.0, layers=32, kv_heads=None)
    idle = _plan(model, devices=[_gpu(7.5)])
    busy = _plan(model, devices=[_gpu(3.0)])
    assert idle.n_gpu_layers == -1
    assert 0 < busy.n_gpu_layers < 32


def test_the_kv_cache_counts_against_the_budget(tmp_path):
    """The bug this covers: 2.3 GB of weights "fit" on an 8 GB card while the
    KV cache for the auto-tuned 131072-token context asked for 18 GB."""
    # 36 layers x 8 kv heads x 256 bytes x 2 = 147456 bytes per token.
    model = _model_file(tmp_path, 2.3, layers=36, kv_heads=8, head_dim=128)
    plan = _plan(model, n_ctx=131072, devices=[_gpu(7.2)])
    assert plan.n_gpu_layers == -1
    assert plan.n_ctx is not None and plan.n_ctx < 131072
    assert plan.requested_n_ctx == 131072
    assert "context reduced" in plan.reason
    # What it settled on must actually fit.
    assert plan.model_bytes + plan.kv_bytes <= plan.free_vram_bytes


def test_a_short_context_that_already_fits_is_left_alone(tmp_path):
    model = _model_file(tmp_path, 2.3, layers=36, kv_heads=8, head_dim=128)
    plan = _plan(model, n_ctx=8192, devices=[_gpu(7.2)])
    assert plan.n_gpu_layers == -1
    assert plan.n_ctx == 8192
    assert "KV cache fit in free VRAM" in plan.reason


def test_the_context_is_never_shrunk_below_the_floor(tmp_path):
    """Past the floor, trade layers instead: a GPU is not worth a context so
    short the conversation cannot hold a turn."""
    model = _model_file(tmp_path, 4.0, layers=36, kv_heads=8, head_dim=128)
    plan = _plan(model, n_ctx=131072, devices=[_gpu(5.2)])
    assert plan.n_ctx >= llama_backend._MIN_GPU_CTX


def test_a_header_without_attention_metadata_sizes_weights_only(tmp_path):
    model = _model_file(tmp_path, 2.0, layers=32, kv_heads=None)
    plan = _plan(model, n_ctx=131072, devices=[_gpu(7.5)])
    assert plan.n_gpu_layers == -1
    assert plan.kv_bytes is None
    assert "could not be sized" in plan.reason


def test_a_pin_leaves_the_context_alone(tmp_path):
    model = _model_file(tmp_path, 2.3, layers=36, kv_heads=8, head_dim=128)
    plan = _plan(model, requested=20, n_ctx=131072, devices=[_gpu(7.2)])
    assert plan.n_gpu_layers == 20
    assert plan.n_ctx == 131072


def test_kv_cache_bytes_matches_llama_cpp(tmp_path):
    """Checked against llama.cpp's own report: Qwen3 4B at 131072 tokens logged
    "Vulkan0 KV buffer size = 18432.00 MiB"."""
    info = {
        "block_count": 36,
        "attention.head_count_kv": 8,
        "attention.key_length": 128,
        "attention.value_length": 128,
    }
    assert llama_backend.kv_cache_bytes(info, 131072) == 18432 * 1024 * 1024


def test_kv_cache_bytes_falls_back_to_embedding_over_heads():
    info = {
        "block_count": 32,
        "attention.head_count_kv": 8,
        "attention.head_count": 32,
        "embedding_length": 4096,
    }
    # head_dim 128, so 32 layers x 8 heads x 256 bytes x 2 = 131072 per token.
    assert llama_backend.kv_cache_bytes(info, 1) == 131072


def test_kv_cache_bytes_is_none_without_enough_metadata():
    assert llama_backend.kv_cache_bytes({"block_count": 32}, 4096) is None
    assert llama_backend.kv_cache_bytes({}, 4096) is None


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _fake_backend_dir(tmp_path: Path, name: str = "lib-vulkan") -> Path:
    pkg = tmp_path / "site-packages" / "llama_cpp"
    (pkg / "lib").mkdir(parents=True, exist_ok=True)
    (pkg / "lib" / "ggml-cpu.dll").write_bytes(b"cpu")
    extra = pkg / name
    extra.mkdir(parents=True, exist_ok=True)
    (extra / "ggml-vulkan.dll").write_bytes(b"vk")
    return pkg


def test_platforms_with_one_backend_set_are_left_alone(monkeypatch):
    monkeypatch.setattr(llama_backend, "available_backends", dict)
    selection = llama_backend.select_backend()
    assert selection.backend == "bundled"
    assert selection.lib_dir is None
    assert llama_backend.LIB_PATH_ENV not in __import__("os").environ


def test_the_cpu_override_skips_the_probe(monkeypatch, tmp_path):
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend, "run_probe", lambda *a, **k: pytest.fail("probe should not run")
    )
    monkeypatch.setenv(llama_backend.BACKEND_ENV, "cpu")
    selection = llama_backend.select_backend()
    assert selection.backend == "cpu"
    assert selection.probed is False


def test_naming_a_backend_selects_it_without_probing(monkeypatch, tmp_path):
    import os

    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend, "run_probe", lambda *a, **k: pytest.fail("probe should not run")
    )
    monkeypatch.setenv(llama_backend.BACKEND_ENV, "vulkan")
    selection = llama_backend.select_backend()
    assert selection.backend == "vulkan"
    assert os.environ[llama_backend.LIB_PATH_ENV] == str(pkg / "lib-vulkan")


def test_a_probe_that_finds_a_gpu_selects_the_gpu_set(monkeypatch, tmp_path):
    import os

    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend,
        "run_probe",
        lambda lib_dir, **k: {
            "loaded": True,
            "supports_gpu_offload": True,
            "devices": [_gpu(7.0), {"name": "CPU", "type": "cpu", "free_bytes": 1, "total_bytes": 2}],
            "error": None,
        },
    )
    selection = llama_backend.select_backend()
    assert selection.backend == "vulkan"
    assert selection.probed is True
    assert os.environ[llama_backend.LIB_PATH_ENV] == str(pkg / "lib-vulkan")
    assert len(selection.gpu_devices) == 1


def test_a_backend_that_cannot_load_falls_back_to_the_cpu(monkeypatch, tmp_path):
    """No Vulkan driver means ggml.dll cannot load at all. Start anyway."""
    import os

    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend,
        "run_probe",
        lambda lib_dir, **k: {"loaded": False, "error": "OSError: vulkan-1.dll not found"},
    )
    selection = llama_backend.select_backend()
    assert selection.backend == "cpu"
    assert llama_backend.LIB_PATH_ENV not in os.environ


def test_a_probe_that_never_answers_falls_back_to_the_cpu(monkeypatch, tmp_path):
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(llama_backend, "run_probe", lambda *a, **k: None)
    assert llama_backend.select_backend().backend == "cpu"


def test_a_backend_that_loads_but_has_no_gpu_is_not_used(monkeypatch, tmp_path):
    """A Vulkan build on a machine with only a software rasteriser is not a GPU."""
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend,
        "run_probe",
        lambda lib_dir, **k: {
            "loaded": True,
            "supports_gpu_offload": True,
            "devices": [{"name": "CPU", "type": "cpu", "free_bytes": 1, "total_bytes": 2}],
            "error": None,
        },
    )
    assert llama_backend.select_backend().backend == "cpu"


def test_the_probe_result_is_cached_and_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(llama_backend.sys, "platform", "win32")
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    calls = []

    def _probe(lib_dir, **kwargs):
        calls.append(lib_dir)
        return {
            "loaded": True,
            "supports_gpu_offload": True,
            "devices": [_gpu(7.0)],
            "error": None,
        }

    monkeypatch.setattr(llama_backend, "run_probe", _probe)
    assert llama_backend.select_backend().backend == "vulkan"
    llama_backend.reset_for_tests()
    assert llama_backend.select_backend().backend == "vulkan"
    assert len(calls) == 1, "the second start should read the cache, not re-probe"


def test_linux_reprobes_after_driver_disappears(monkeypatch, tmp_path):
    monkeypatch.setattr(llama_backend.sys, "platform", "linux")
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(llama_backend, "run_probe", lambda *a, **k: {
        "loaded": True, "supports_gpu_offload": True, "devices": [_gpu(7)], "error": None,
    })
    assert llama_backend.select_backend().backend == "vulkan"
    llama_backend.reset_for_tests()
    monkeypatch.setattr(llama_backend, "run_probe", lambda *a, **k: None)
    assert llama_backend.select_backend().backend == "cpu"
    assert llama_backend.LIB_PATH_ENV not in __import__("os").environ


def test_linux_finds_the_isolated_wheel_layout(monkeypatch, tmp_path):
    pkg = tmp_path / "llama_cpp"
    nested = pkg / "lib-vulkan" / "llama_cpp" / "lib"
    nested.mkdir(parents=True)
    (nested / "libllama.so").write_bytes(b"native")
    monkeypatch.setattr(llama_backend, "llama_cpp_package_dir", lambda: pkg)
    assert llama_backend.available_backends() == {"vulkan": nested}


def test_probe_crashing_after_json_is_not_accepted(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(llama_backend.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, -11, b'{"loaded": true, "supports_gpu_offload": true}', b"driver crash",
    ))
    assert llama_backend.run_probe(tmp_path) is None


def test_rebuilding_the_bundle_invalidates_the_cache(monkeypatch, tmp_path):
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    calls = []

    def _probe(lib_dir, **kwargs):
        calls.append(lib_dir)
        return {"loaded": True, "supports_gpu_offload": True, "devices": [_gpu(7.0)], "error": None}

    monkeypatch.setattr(llama_backend, "run_probe", _probe)
    llama_backend.select_backend()
    llama_backend.reset_for_tests()

    # A different backend payload is a different build.
    (pkg / "lib-vulkan" / "ggml-vulkan.dll").write_bytes(b"vk-rebuilt-larger")
    llama_backend.select_backend()
    assert len(calls) == 2


def test_the_cache_file_records_what_was_probed(monkeypatch, tmp_path):
    pkg = _fake_backend_dir(tmp_path)
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    monkeypatch.setattr(
        llama_backend,
        "run_probe",
        lambda lib_dir, **k: {
            "loaded": True,
            "supports_gpu_offload": True,
            "devices": [_gpu(7.0)],
            "error": None,
        },
    )
    llama_backend.select_backend()
    cached = json.loads(llama_backend._cache_path().read_text(encoding="utf-8"))
    assert cached["backend"] == "vulkan"
    assert cached["devices"][0]["description"] == "Test GPU"
