"""Hardware detection for model tier recommendations.

Implemented fully per guide §4.2 — pure Python stdlib + psutil, no heavy deps.

GPU detection paths, tried in order of specificity:
  - NVIDIA via GPUtil          (Windows/Linux with nvidia-smi available)
  - Apple Silicon via system_profiler  (macOS arm64)
  - Windows generic via PowerShell Win32_VideoController
      (catches AMD/Intel/NVIDIA when GPUtil is unavailable or the card is
       not NVIDIA — e.g. Radeon, Arc, integrated graphics)

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
    # VRAM, while Win32_VideoController.AdapterRAM is capped at 4 GiB by a
    # signed-int32 quirk on some systems).
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
    """Return {"gpu_name", "gpu_vram_gb"} for the primary Windows adapter, or {}.

    Queries Win32_VideoController via PowerShell. Picks the adapter with the
    most VRAM when more than one is present (laptops with integrated + dGPU).
    Falls back to powershell.exe -> pwsh.exe -> nothing.

    AdapterRAM caveat: Win32_VideoController.AdapterRAM is stored as a UInt32
    on many systems, so cards with >= 4 GB report exactly 4 GiB. The name is
    still accurate, so the user gets identification even when the VRAM number
    underreports. (NVIDIA users get the real VRAM via the GPUtil path above.)
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return {}

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
        return {}

    if result.returncode != 0 or not result.stdout.strip():
        return {}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    # Single-controller hosts get a dict; multi-controller hosts get a list.
    controllers = data if isinstance(data, list) else [data]
    if not controllers:
        return {}

    def _vram_bytes(c: dict) -> int:
        v = c.get("AdapterRAM")
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    primary = max(controllers, key=_vram_bytes)
    name = primary.get("Name")
    vram = _vram_bytes(primary)
    if not isinstance(name, str) or not name:
        return {}

    out: dict[str, object] = {"gpu_name": name.strip()}
    if vram > 0:
        out["gpu_vram_gb"] = round(vram / (1024 ** 3), 1)
    return out
