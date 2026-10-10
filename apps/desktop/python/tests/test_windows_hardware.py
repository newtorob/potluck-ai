"""Windows hardware detection reports the real dedicated VRAM.

Recorded on the RX 6600 XT machine, 2026-10-09 (previous display driver):

    Win32_VideoController   {"Name":"AMD Radeon RX 6600 XT","AdapterRAM":4293918720}
    registry qwMemorySize   8573157376   (HardwareInformation.qwMemorySize)
    Vulkan probe total      8573157376

AdapterRAM is a 32-bit field, so the card read 4.0 GB and the app's model
suggestion was sized for a 4 GB card.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from potluck_local.services import hardware_detect, llama_backend, windows_gpu

GIB = 1024 ** 3
RX = "AMD Radeon RX 6600 XT"
ADAPTER_RAM_CAPPED = 4_293_918_720
REAL_BYTES = 8_573_157_376
WMI_ONE = [{"Name": RX, "AdapterRAM": ADAPTER_RAM_CAPPED}]
REGISTRY_ONE = [{"name": RX, "dedicated_bytes": REAL_BYTES}]
PROBE_ONE = [{"name": "Vulkan0", "description": RX, "type": "gpu", "free_bytes": 7_712_657_152, "total_bytes": REAL_BYTES}]


@pytest.fixture
def sources(monkeypatch):
    """Replace the three sources; each test sets what each one returns."""
    state = SimpleNamespace(wmi=[], registry=[], probe=[])
    monkeypatch.setattr(hardware_detect, "_win32_video_controllers", lambda: state.wmi)
    monkeypatch.setattr(hardware_detect, "_registry_adapters", lambda: state.registry)
    monkeypatch.setattr(hardware_detect, "_probed_gpu_devices", lambda: state.probe)
    return state


def test_a_card_over_4_gb_reads_its_real_size_from_the_registry(sources):
    sources.wmi, sources.registry = WMI_ONE, REGISTRY_ONE
    assert hardware_detect._detect_windows_gpu() == {
        "gpu_name": RX, "gpu_vram_gb": 8.0, "gpu_vram_source": "registry",
    }


def test_without_the_registry_value_the_engines_probe_total_is_next(sources):
    sources.wmi, sources.probe = WMI_ONE, PROBE_ONE
    assert hardware_detect._detect_windows_gpu() == {
        "gpu_name": RX, "gpu_vram_gb": 8.0, "gpu_vram_source": "engine_probe",
    }


def test_with_neither_the_old_adapter_ram_path_still_answers(sources):
    sources.wmi = WMI_ONE
    assert hardware_detect._detect_windows_gpu() == {
        "gpu_name": RX, "gpu_vram_gb": 4.0, "gpu_vram_source": "adapter_ram",
    }


def test_the_registry_wins_over_the_probe_and_the_probe_over_adapter_ram(sources):
    sources.wmi, sources.registry = WMI_ONE, [{"name": RX, "dedicated_bytes": 8 * GIB}]
    sources.probe = [{**PROBE_ONE[0], "total_bytes": 7 * GIB}]
    assert hardware_detect._detect_windows_gpu()["gpu_vram_gb"] == 8.0


def test_the_biggest_real_card_is_primary_when_there_are_two_adapters(sources):
    sources.wmi = [
        {"Name": "Intel(R) UHD Graphics 770", "AdapterRAM": 1_073_741_824},
        {"Name": RX, "AdapterRAM": ADAPTER_RAM_CAPPED},
    ]
    sources.registry = [
        {"name": "Intel(R) UHD Graphics 770", "dedicated_bytes": 128 * 1024 ** 2},
        {"name": RX, "dedicated_bytes": REAL_BYTES},
    ]
    found = hardware_detect._detect_windows_gpu()
    assert found["gpu_name"] == RX and found["gpu_vram_gb"] == 8.0


def test_a_registry_entry_for_another_card_is_not_applied_to_this_one(sources):
    sources.wmi = WMI_ONE
    sources.registry = [{"name": "AMD Radeon RX 6600", "dedicated_bytes": 8 * GIB}]
    assert hardware_detect._detect_windows_gpu()["gpu_vram_source"] == "adapter_ram"


def test_the_registry_alone_names_the_card_when_powershell_is_missing(sources):
    sources.registry = REGISTRY_ONE
    assert hardware_detect._detect_windows_gpu() == {
        "gpu_name": RX, "gpu_vram_gb": 8.0, "gpu_vram_source": "registry",
    }


def test_nothing_readable_gives_nothing(sources):
    assert hardware_detect._detect_windows_gpu() == {}


@pytest.fixture
def windows_host(monkeypatch):
    import psutil

    monkeypatch.setattr(hardware_detect.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hardware_detect.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=8 * GIB, available=5 * GIB))
    monkeypatch.setitem(sys.modules, "GPUtil", SimpleNamespace(getGPUs=lambda: []))


def test_the_model_suggestion_follows_the_real_vram(windows_host, sources):
    """On an 8 GB RAM machine the tier is decided by VRAM: 4.0 GB suggests small, 8.0 GB medium."""
    sources.wmi = WMI_ONE
    capped = hardware_detect.detect_hardware()
    assert (capped["gpu_vram_gb"], capped["recommended_tier"]) == (4.0, "small")

    sources.registry = REGISTRY_ONE
    real = hardware_detect.detect_hardware()
    assert real["gpu_name"] == RX
    assert (real["gpu_vram_gb"], real["gpu_vram_source"], real["recommended_tier"]) == (8.0, "registry", "medium")


def test_a_working_nvidia_reading_still_takes_precedence(windows_host, sources, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "GPUtil",
        SimpleNamespace(getGPUs=lambda: [SimpleNamespace(name="NVIDIA GeForce RTX 2080 Ti", memoryTotal=11264)]),
    )
    sources.wmi, sources.registry = WMI_ONE, REGISTRY_ONE
    info = hardware_detect.detect_hardware()
    assert info["gpu_name"] == "NVIDIA GeForce RTX 2080 Ti" and info["gpu_vram_gb"] == 11.0


def test_other_platforms_report_no_vram_source(monkeypatch):
    import psutil

    monkeypatch.setattr(hardware_detect.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware_detect.platform, "machine", lambda: "x86_64")
    monkeypatch.setitem(sys.modules, "GPUtil", SimpleNamespace(getGPUs=lambda: []))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(total=8 * GIB, available=5 * GIB))
    info = hardware_detect.detect_hardware()
    assert info["gpu_vram_source"] is None


# ---------------------------------------------------------------------------
# The probe total, through the real selection object
# ---------------------------------------------------------------------------


def test_the_probe_source_reads_gpu_devices_from_a_selected_backend(monkeypatch):
    monkeypatch.setattr(
        llama_backend, "current_selection",
        lambda: llama_backend.Selection("vulkan", None, "probe", [PROBE_ONE[0]], True),
    )
    assert hardware_detect._probed_gpu_devices() == PROBE_ONE
    monkeypatch.setattr(
        llama_backend, "current_selection", lambda: llama_backend.Selection("cpu", None, "no gpu", [], True)
    )
    assert hardware_detect._probed_gpu_devices() == []


# ---------------------------------------------------------------------------
# The registry reader, against a recorded layout
# ---------------------------------------------------------------------------


class FakeRegistry:
    """The display class key as the machine returned it, plus the odd shapes drivers write."""

    HKEY_LOCAL_MACHINE = object()

    def __init__(self, tree: dict[str, dict]) -> None:
        self.tree = tree

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, parent, sub):  # noqa: N802
        if parent is self.HKEY_LOCAL_MACHINE:
            return self._Key(self.tree)
        if sub not in parent.values:
            raise OSError(2, "not found")
        return self._Key(parent.values[sub])

    def EnumKey(self, key, index):  # noqa: N802
        names = list(key.values)
        if index >= len(names):
            raise OSError(259, "no more")
        return names[index]

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.values:
            raise OSError(2, "not found")
        return key.values[name], 0


def test_the_registry_reader_returns_qword_and_binary_sizes_and_skips_the_rest(monkeypatch):
    fake = FakeRegistry({
        "0000": {"DriverDesc": RX, "HardwareInformation.qwMemorySize": REAL_BYTES},
        "0001": {"DriverDesc": "Microsoft Basic Display Adapter"},  # no size
        "0002": {  # a driver that writes REG_BINARY, and the name only as a UTF-16 adapter string
            "HardwareInformation.AdapterString": "NVIDIA GeForce RTX 2080 Ti".encode("utf-16-le") + b"\x00\x00",
            "HardwareInformation.qwMemorySize": (11 * GIB).to_bytes(8, "little"),
        },
        "Configuration": {"DriverDesc": "ignored", "HardwareInformation.qwMemorySize": 1},
        "Properties": {},
    })
    monkeypatch.setattr(windows_gpu, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert windows_gpu.registry_adapter_memory() == [
        {"name": RX, "dedicated_bytes": REAL_BYTES},
        {"name": "NVIDIA GeForce RTX 2080 Ti", "dedicated_bytes": 11 * GIB},
    ]


def test_an_unreadable_registry_gives_an_empty_list(monkeypatch):
    class Denied(FakeRegistry):
        def OpenKey(self, parent, sub):  # noqa: N802
            raise PermissionError(5, "access denied")

    monkeypatch.setattr(windows_gpu, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(sys.modules, "winreg", Denied({}))
    assert windows_gpu.registry_adapter_memory() == []


def test_off_windows_the_registry_reader_does_nothing():
    if sys.platform == "win32":
        pytest.skip("runs on Windows")
    assert windows_gpu.registry_adapter_memory() == []


def test_the_new_modules_import_cleanly_and_stay_torch_free_off_windows():
    """Nothing here touches ctypes.windll or winreg at import, and nothing pulls in torch."""
    import subprocess
    from pathlib import Path

    code = (
        "import sys\n"
        "from potluck_local.services import windows_gpu, llama_backend, hardware_detect, inference_errors\n"
        "bad = [m for m in ('torch', 'transformers', 'winreg') if m in sys.modules]\n"
        "assert not bad, bad\n"
        "assert windows_gpu.read_adapter_memory().status in ('not_applicable', 'ok', 'unavailable')\n"
    )
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                          env={**__import__("os").environ, "PYTHONPATH": str(root)})
    assert done.returncode == 0, done.stderr
