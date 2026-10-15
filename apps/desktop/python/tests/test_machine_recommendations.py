"""Cold-start recommendations vary with the machine, never replace local proof."""
import json
import math

import pytest

from test_measured_fit import Lab


def catalog(lab):
    for slug, size, tier, vram in [("small", 2, "small", 4), ("medium", 5, "medium", 6), ("large", 18, "large", 20)]:
        lab.model(slug, size, featured=True)
        lab.entries[-1].update(tier=tier, min_vram_gb=vram)


@pytest.mark.parametrize("hardware,expected", [
    ({"os": "Darwin", "arch": "arm64", "ram_total_gb": 16, "ram_available_gb": 10, "gpu_vram_gb": 16}, "medium"),
    ({"os": "Darwin", "arch": "arm64", "ram_total_gb": 64, "gpu_vram_gb": 64}, "large"),
    ({"os": "Windows", "gpu_vram_gb": 11}, "medium"),
    ({"os": "Windows", "gpu_vram_gb": 24}, "large"),
    ({"os": "Windows", "gpu_vram_gb": 24, "inference_gpu_available": False}, "small"),
    ({"os": "Linux", "gpu_vram_gb": 24, "inference_gpu_available": False}, "small"),
    ({"os": "Linux", "gpu_vram_gb": 24, "inference_gpu_available": None}, "small"),
    ({"os": "Linux", "gpu_vram_gb": 24, "inference_gpu_available": True, "inference_gpu_vram_gb": 8}, "medium"),
    ({"os": "Linux", "gpu_vram_gb": 24, "inference_gpu_available": True, "inference_gpu_vram_gb": None}, "small"),
    ({"os": "Windows", "gpu_vram_gb": math.nan}, "small"),
    ({"os": "Windows", "gpu_vram_gb": None}, "small"),
])
def test_prefers_a_catalog_tier_within_gpu_capacity_or_a_small_cpu_model(tmp_path, monkeypatch, hardware, expected):
    lab = Lab(tmp_path, monkeypatch, available=50, total=64)
    lab.hardware.update(hardware)
    catalog(lab)
    out = lab.options()
    assert out["recommended"][0]["id"] == expected
    assert all(p["basis"] == "estimate" and p["tokensPerSecond"] is None for p in out["recommended"])
    assert next(m for m in out["models"] if m["id"] == expected)["recommendationReason"]


def test_available_unified_memory_limits_picks_without_adding_vram_to_ram(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=6, total=16)
    lab.hardware.update(os="Darwin", arch="arm64", gpu_vram_gb=16)
    catalog(lab)
    assert lab.picks() == ["small"]


def test_proof_still_beats_a_larger_unmeasured_gpu_pick(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=50, total=64)
    lab.hardware.update(os="Darwin", arch="arm64", gpu_vram_gb=64)
    catalog(lab)
    lab.prove("small", host=2)
    lab.bench("small", 30)
    assert lab.picks() == ["small", "large", "medium"]


@pytest.mark.parametrize("available", [math.nan, math.inf, -1, True, None])
def test_unknown_or_invalid_available_memory_never_yields_a_cold_pick(tmp_path, monkeypatch, available):
    lab = Lab(tmp_path, monkeypatch, available=available, total=64)
    catalog(lab)
    assert lab.picks() == []
    out = lab.options()
    assert out["hardware"]["ram_available_gb"] is None
    json.dumps(out, allow_nan=False)


def test_recommendation_does_not_load_or_download(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=10)
    catalog(lab)
    # The runtime stub only supports reading state; no load/unload/generate methods.
    before = {p: p.stat().st_mtime_ns for p in lab.base.rglob("*")}
    assert lab.options()["recommended"]
    assert {p: p.stat().st_mtime_ns for p in lab.base.rglob("*")} == before
    assert lab.loaded is None
