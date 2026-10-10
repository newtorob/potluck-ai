"""The driver fingerprint: short, changes with the driver, never raises."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from potluck_local.services import driver_fingerprint as df
from potluck_local.services import llama_backend
from test_windows_hardware import FakeRegistry, RX

NVIDIA_PROC = (
    "NVRM version: NVIDIA UNIX x86_64 Kernel Module  560.35.03  Fri Aug 16 21:39:15 UTC 2024\n"
    "GCC version:  gcc version 13.2.0 (Ubuntu 13.2.0-23ubuntu4)\n"
)


def windows(monkeypatch, tree) -> None:
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "winreg", FakeRegistry(tree))


def adapter(name: str, version: str | None, date: str | None = None) -> dict:
    out = {"DriverDesc": name}
    if version:
        out["DriverVersion"] = version
    if date:
        out["DriverDate"] = date
    return out


# ---- Windows -----------------------------------------------------------------


def test_windows_reads_the_version_and_date_of_the_adapter_matched_by_name(monkeypatch):
    windows(monkeypatch, {
        "0000": adapter("Microsoft Basic Display Adapter", "10.0.1"),
        "0001": adapter(RX, "31.0.21025.10", "2-11-2025"),
        "0002": adapter("Intel(R) UHD Graphics 770", "31.0.101.4502", "1-3-2024"),
        "Configuration": {}, "Properties": {},
    })
    assert df.driver_fingerprint(RX) == "31.0.21025.10 (2-11-2025)"
    assert df.driver_fingerprint("Intel UHD Graphics 770") == "31.0.101.4502 (1-3-2024)"


def test_a_new_driver_gives_a_different_fingerprint(monkeypatch):
    windows(monkeypatch, {"0000": adapter(RX, "31.0.21025.10", "2-11-2025")})
    old = df.driver_fingerprint(RX)
    windows(monkeypatch, {"0000": adapter(RX, "32.0.12011.1009", "9-3-2025")})
    assert df.driver_fingerprint(RX) != old


def test_windows_with_no_name_joins_the_hardware_adapters_and_leaves_out_the_basic_one(monkeypatch):
    windows(monkeypatch, {
        "0000": adapter("Microsoft Basic Display Adapter", "10.0.1"),
        "0001": adapter(RX, "31.0.21025.10", "2-11-2025"),
        "0002": adapter("Intel(R) UHD Graphics 770", "31.0.101.4502"),
    })
    assert df.driver_fingerprint() == "31.0.101.4502; 31.0.21025.10 (2-11-2025)"


def test_windows_does_not_guess_when_the_name_matches_nothing_or_two_drivers(monkeypatch):
    windows(monkeypatch, {"0000": adapter(RX, "31.0.1"), "0001": adapter(RX, "31.0.2")})
    assert df.driver_fingerprint(RX) is None
    assert df.driver_fingerprint("Some Other Card") is None


def test_windows_without_a_version_value_is_unknown(monkeypatch):
    windows(monkeypatch, {"0000": adapter(RX, None)})
    assert df.driver_fingerprint(RX) is None


def test_windows_with_an_unreadable_registry_is_unknown(monkeypatch):
    class Denied(FakeRegistry):
        def OpenKey(self, parent, sub):  # noqa: N802
            raise PermissionError(5, "access denied")

    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "winreg", Denied({}))
    assert df.driver_fingerprint(RX) is None


# ---- Linux ---------------------------------------------------------------------


@pytest.fixture()
def linux(monkeypatch, tmp_path):
    """A fake /proc and /sys: write the files a machine would have."""
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(df, "_NVIDIA_VERSION_FILE", tmp_path / "proc" / "nvidia_version")
    monkeypatch.setattr(df, "_DRM_DIR", tmp_path / "sys" / "class" / "drm")
    monkeypatch.setattr(df, "_MODULE_DIR", tmp_path / "sys" / "module")
    monkeypatch.setattr(df.platform, "release", lambda: "6.8.0-45-generic")
    (tmp_path / "proc").mkdir()
    (tmp_path / "sys" / "class" / "drm").mkdir(parents=True)
    (tmp_path / "sys" / "module").mkdir(parents=True)

    class Machine:
        root = tmp_path

        def nvidia(self, text=NVIDIA_PROC):
            (tmp_path / "proc" / "nvidia_version").write_text(text)

        def card(self, n: int, driver: str, version: str | None = None):
            card = tmp_path / "sys" / "class" / "drm" / f"card{n}" / "device"
            card.mkdir(parents=True)
            (card / "driver").symlink_to(Path("/sys/bus/pci/drivers") / driver)
            if version:
                (tmp_path / "sys" / "module" / driver).mkdir(exist_ok=True)
                (tmp_path / "sys" / "module" / driver / "version").write_text(version + "\n")

    return Machine()


def test_linux_reads_the_nvidia_kernel_module_version(linux):
    linux.nvidia()
    assert df.driver_fingerprint("NVIDIA GeForce RTX 2080 Ti") == "nvidia 560.35.03"
    assert df.driver_fingerprint() == "nvidia 560.35.03"


def test_linux_without_the_proc_file_uses_the_driver_bound_to_the_card(linux):
    linux.card(0, "amdgpu")
    assert df.driver_fingerprint("AMD Radeon RX 6600 XT") == "amdgpu (kernel 6.8.0-45-generic)"


def test_linux_uses_the_module_version_when_the_driver_publishes_one(linux):
    linux.card(0, "amdgpu", "6.7.0")
    assert df.driver_fingerprint("AMD Radeon RX 6600 XT") == "amdgpu 6.7.0"


def test_linux_picks_the_driver_for_the_named_vendor_among_several_cards(linux):
    linux.card(0, "i915")
    linux.card(1, "amdgpu")
    assert df.driver_fingerprint("AMD Radeon RX 6600 XT") == "amdgpu (kernel 6.8.0-45-generic)"
    assert df.driver_fingerprint("Intel(R) Arc(TM) A770") == "i915 (kernel 6.8.0-45-generic)"
    assert df.driver_fingerprint() == "i915 (kernel 6.8.0-45-generic); amdgpu (kernel 6.8.0-45-generic)"


def test_linux_with_nothing_to_read_gives_the_kernel_release(linux):
    assert df.driver_fingerprint("Mystery Card") == "kernel 6.8.0-45-generic"


def test_linux_with_an_unrecognised_proc_file_keeps_its_first_line(linux):
    linux.nvidia("something new\nsecond line\n")
    assert df.driver_fingerprint("NVIDIA GeForce RTX 2080 Ti") == "something new"


# ---- macOS and the rest -----------------------------------------------------------


def test_macos_is_the_os_version_and_build(monkeypatch):
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(df.platform, "mac_ver", lambda: ("15.7.3", ("", "", ""), "arm64"))
    monkeypatch.setattr(df, "_sysctl_string", lambda name: "24G419")
    assert df.driver_fingerprint("Apple M2 Pro") == "macOS 15.7.3 (24G419)"
    monkeypatch.setattr(df, "_sysctl_string", lambda name: None)
    assert df.driver_fingerprint() == "macOS 15.7.3"


def test_an_unknown_platform_has_no_fingerprint(monkeypatch):
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="plan9"))
    assert df.driver_fingerprint() is None


def test_it_never_raises(monkeypatch):
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(df, "_bound_display_drivers", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert df.driver_fingerprint("AMD Radeon") is None


def test_the_result_is_short_and_on_one_line(monkeypatch):
    monkeypatch.setattr(df, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(df, "_macos", lambda: "line one\n" + "x" * 500)
    value = df.driver_fingerprint()
    assert len(value) <= 120 and "\n" not in value


def test_the_real_machine_gives_a_string_or_none():
    assert df.driver_fingerprint() is None or isinstance(df.driver_fingerprint(), str)


# ---- the probe cache -----------------------------------------------------------------


def test_a_driver_update_changes_the_probe_cache_signature(monkeypatch, tmp_path):
    (tmp_path / "ggml-vulkan.dll").write_bytes(b"vk")
    monkeypatch.setattr(llama_backend, "driver_fingerprint", lambda *a: "31.0.1")
    before = llama_backend._signature({"vulkan": tmp_path})
    monkeypatch.setattr(llama_backend, "driver_fingerprint", lambda *a: "31.0.2")
    assert llama_backend._signature({"vulkan": tmp_path}) != before


def test_a_driver_update_reprobes_a_cached_selection(monkeypatch, tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "lib-vulkan").mkdir(parents=True)
    (pkg / "lib-vulkan" / "ggml-vulkan.dll").write_bytes(b"vk")
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(llama_backend.sys, "platform", "win32")
    monkeypatch.setattr(llama_backend, "available_backends", lambda: {"vulkan": pkg / "lib-vulkan"})
    gpu = {"name": "Vulkan0", "description": "Test GPU", "type": "gpu", "free_bytes": 1, "total_bytes": 2}
    calls = []
    monkeypatch.setattr(llama_backend, "run_probe", lambda lib_dir, **k: calls.append(lib_dir) or {
        "loaded": True, "supports_gpu_offload": True, "devices": [gpu], "error": None})
    monkeypatch.setattr(llama_backend, "driver_fingerprint", lambda *a: "31.0.1")
    llama_backend.reset_for_tests()
    llama_backend.select_backend()
    llama_backend.reset_for_tests()
    llama_backend.select_backend()
    assert len(calls) == 1  # the cache holds while the driver is the same
    monkeypatch.setattr(llama_backend, "driver_fingerprint", lambda *a: "31.0.2")
    llama_backend.reset_for_tests()
    llama_backend.select_backend()
    assert len(calls) == 2
    llama_backend.reset_for_tests()
