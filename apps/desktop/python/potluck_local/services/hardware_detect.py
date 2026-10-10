"""Hardware detection for model tier recommendations.

Implemented fully per guide §4.2 — pure Python stdlib + psutil, no heavy deps.

GPU detection paths, tried in order of specificity:
  - NVIDIA via GPUtil          (Windows/Linux with nvidia-smi available)
  - Apple Silicon via system_profiler  (macOS arm64)
  - Windows generic via PowerShell Win32_VideoController
      (catches AMD/Intel/NVIDIA when GPUtil is unavailable or the card is
       not NVIDIA — e.g. Radeon, Arc, integrated graphics). The VRAM figure
       comes from the display driver's 64-bit registry value first, then from
       the engine's own backend probe, then from AdapterRAM (see
       _detect_windows_gpu for why AdapterRAM comes last).

Each path is wrapped so missing tools degrade silently to the next.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess


def detect_hardware() -> dict[str, object]:
    """Detect CPU, RAM, and GPU capabilities on the host machine.

    Returns a dict suitable for direct serialisation in the /hardware response.
    All fields are present; GPU fields are None when not detected.

    Recommended tier logic mirrors the guide:
      large  — VRAM >= 24 GB, or Apple Silicon with >= 64 GB RAM
      medium — VRAM >= 8 GB, or RAM >= 16 GB
      small  — everything else
    """
    import psutil  # imported here to keep the module importable without psutil

    info: dict[str, object] = {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_name": platform.processor() or "Unknown",
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "ram_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
        "ram_available_gb": round(psutil.virtual_memory().available / (1024 ** 3), 1),
        "gpu_name": None,
        "gpu_vram_gb": None,
        "gpu_vram_source": None,
        "recommended_tier": "small",
    }

    # NVIDIA GPU detection via GPUtil
    try:
        import GPUtil  # type: ignore[import-untyped]

        gpus = GPUtil.getGPUs()
        if gpus:
            gpu = gpus[0]
            info["gpu_name"] = gpu.name
            info["gpu_vram_gb"] = round(gpu.memoryTotal / 1024, 1)
    except Exception:
        pass

    # Windows generic detection via PowerShell Win32_VideoController.
    # Runs only when NVIDIA detection didn't already populate gpu_name, so a
    # working NVIDIA reading from GPUtil takes precedence (it reports live
    # VRAM, while Win32_VideoController.AdapterRAM is a 32-bit field that
    # cannot report more than 4 GiB).
    if info["gpu_name"] is None and platform.system() == "Windows":
        info.update(_detect_windows_gpu())

    # Apple Silicon via system_profiler (Metal unified memory)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            displays = data.get("SPDisplaysDataType", [])
            if displays:
                gpu_info = displays[0]
                info["gpu_name"] = gpu_info.get("sppci_model", "Apple Silicon")
                # Unified memory: the full RAM pool is available to the GPU
                info["gpu_vram_gb"] = info["ram_total_gb"]
        except Exception:
            info["gpu_name"] = "Apple Silicon (detection failed)"
            info["gpu_vram_gb"] = info["ram_total_gb"]

    # Installed hardware and an inference-capable backend are separate facts.
    # Vulkan also supplies AMD/Intel device details when nvidia-smi cannot.
    if platform.system() == "Linux":
        from potluck_local.services.llama_backend import current_selection, offload_device

        selection = current_selection()
        verified = selection.probed or selection.backend == "cpu"
        devices = selection.gpu_devices if selection.backend != "cpu" else []
        selected = offload_device(devices)
        info["inference_gpu_available"] = bool(devices) if verified else None
        info["inference_gpu_vram_gb"] = (
            round(int(selected[1].get("total_bytes") or 0) / (1024 ** 3), 1)
            if selected else None
        )
        if selected and info["gpu_name"] is None:
            primary = selected[1]
            info["gpu_name"] = primary.get("description") or primary.get("name")
            info["gpu_vram_gb"] = info["inference_gpu_vram_gb"]

    # Linux recommendations use verified inference capacity; the physical
    # adapter stays visible even when missing drivers force CPU execution.
    capacity = info["gpu_vram_gb"]
    if platform.system() == "Linux":
        capacity = info.get("inference_gpu_vram_gb") if info.get("inference_gpu_available") else None
    vram = float(capacity) if capacity is not None else 0.0
    ram = float(info["ram_total_gb"])  # type: ignore[arg-type]

    if vram >= 24 or (platform.machine() == "arm64" and ram >= 64):
        info["recommended_tier"] = "large"
    elif vram >= 8 or ram >= 16:
        info["recommended_tier"] = "medium"
    else:
        info["recommended_tier"] = "small"

    return info


def _detect_windows_gpu() -> dict[str, object]:
    """Return {"gpu_name", "gpu_vram_gb", "gpu_vram_source"} for the primary Windows adapter, or {}.

    The adapter list comes from Win32_VideoController through PowerShell
    (powershell.exe -> pwsh.exe -> nothing). The dedicated memory figure for each
    adapter comes from, in order:

    1. ``HardwareInformation.qwMemorySize`` under the display adapter's class
       key in the registry. The display driver writes it as a 64-bit value, so
       an 8 GiB card reads 8 GiB.
    2. The engine's own backend probe (the device total ggml reported for an
       adapter of the same name), when the engine has selected a GPU backend.
    3. ``Win32_VideoController.AdapterRAM``. This is a 32-bit field: every card
       over 4 GiB reads about 4 GiB (an RX 6600 XT read 4,293,918,720 bytes while
       the registry and the Vulkan probe both said 8,573,157,376 on 2026-10-09).
       It stays as the last resort because it is the only source that exists on
       every Windows install.

    The adapter with the most memory by that ordering is the primary one
    (laptops with an integrated GPU and a discrete one).
    """
    controllers = _win32_video_controllers()
    registry = _registry_adapters()
    probed = _probed_gpu_devices()

    def adapter_ram(c: dict) -> int:
        v = c.get("AdapterRAM")
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    def find(entries: list[dict], name: str, name_key: str, size_key: str) -> int:
        from potluck_local.services.windows_gpu import names_match

        for entry in entries:
            if names_match(name, entry.get(name_key)):
                return int(entry.get(size_key) or 0)
        return 0

    candidates: list[tuple[str, int, str]] = []
    for c in controllers:
        name = c.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        ram = adapter_ram(c)
        from_registry = find(registry, name, "name", "dedicated_bytes")
        from_probe = find(probed, name, "description", "total_bytes")
        if from_registry:
            candidates.append((name, from_registry, "registry"))
        elif from_probe:
            candidates.append((name, from_probe, "engine_probe"))
        else:
            candidates.append((name, ram, "adapter_ram"))

    if not candidates and not controllers and registry:
        # PowerShell unavailable: the registry alone still names the adapters.
        candidates = [(e["name"], int(e["dedicated_bytes"]), "registry") for e in registry]

    if not candidates:
        return {}
    name, vram, source = max(candidates, key=lambda item: item[1])
    out: dict[str, object] = {"gpu_name": name}
    if vram > 0:
        out["gpu_vram_gb"] = round(vram / (1024 ** 3), 1)
        out["gpu_vram_source"] = source
    return out


def _win32_video_controllers() -> list[dict]:
    """Win32_VideoController rows ({"Name", "AdapterRAM"}), or [] when unreadable."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return []

    # ConvertTo-Json -Compress emits one line of JSON; -NoProfile skips $PROFILE
    # so we don't inherit slow user-profile init time.
    script = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,AdapterRAM | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    # Single-controller hosts get a dict; multi-controller hosts get a list.
    controllers = data if isinstance(data, list) else [data]
    return [c for c in controllers if isinstance(c, dict)]


def _registry_adapters() -> list[dict]:
    """Display adapters with a 64-bit memory size from the driver's registry key."""
    try:
        from potluck_local.services.windows_gpu import registry_adapter_memory

        return registry_adapter_memory()
    except Exception:  # noqa: BLE001 - detection degrades to the next source
        return []


def _probed_gpu_devices() -> list[dict]:
    """GPU devices the engine's backend probe reported, or [] when it found none."""
    try:
        from potluck_local.services.llama_backend import current_selection

        selection = current_selection()
        return list(selection.gpu_devices) if selection.backend != "cpu" else []
    except Exception:  # noqa: BLE001
        return []
