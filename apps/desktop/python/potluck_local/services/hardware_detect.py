"""Hardware detection for model tier recommendations.

Implemented fully per guide §4.2 — pure Python stdlib + psutil, no heavy deps.

GPU paths (GPUtil for NVIDIA, system_profiler for Apple Silicon) are
attempted but silently skipped if unavailable, so this module imports cleanly
even before the full requirements are installed.
"""

from __future__ import annotations

import json
import platform
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

    # Tier recommendation
    vram = float(info["gpu_vram_gb"]) if info["gpu_vram_gb"] is not None else 0.0
    ram = float(info["ram_total_gb"])  # type: ignore[arg-type]

    if vram >= 24 or (platform.machine() == "arm64" and ram >= 64):
        info["recommended_tier"] = "large"
    elif vram >= 8 or ram >= 16:
        info["recommended_tier"] = "medium"
    else:
        info["recommended_tier"] = "small"

    return info
