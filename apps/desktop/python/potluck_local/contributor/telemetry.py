"""Live capacity and static hardware facts the contributor reports.

Two callers: the standalone ``potluck-contributor`` script and the in-process
``ContributorService`` inside the sidecar. Both send exactly what these
helpers return, nothing else.

Privacy: everything here is machine capacity. No member id, no hostname
(that travels separately and only in attestation), no prompt content.
"""
from __future__ import annotations

import logging
import platform as _platform
from dataclasses import dataclass

logger = logging.getLogger("potluck.contributor.telemetry")


@dataclass(frozen=True)
class Capacity:
    """One heartbeat's worth of live capacity.

    ``gpu_utilization`` is a fraction in [0, 1] (GPUtil's ``load``), or None
    when no GPU reading exists. ``vram_available_gb`` is free dedicated VRAM
    on NVIDIA, free unified memory on Apple Silicon, or None.
    """

    gpu_utilization: float | None = None
    vram_available_gb: float | None = None


def probe_capacity() -> Capacity:
    """Read live GPU capacity. Never raises: every backend is best effort and
    a machine with no readable GPU reports both fields as None.

    NVIDIA first (GPUtil shells out to nvidia-smi, tens of milliseconds), then
    Apple Silicon where the GPU shares system memory so free RAM is the honest
    VRAM figure. AMD and Intel on Windows have no cheap reading yet.
    """
    try:
        import GPUtil  # type: ignore[import-untyped]

        gpus = GPUtil.getGPUs()
        if gpus:
            gpu = gpus[0]
            load = gpu.load
            free_mb = gpu.memoryFree
            return Capacity(
                gpu_utilization=(
                    max(0.0, min(1.0, float(load))) if load is not None else None
                ),
                vram_available_gb=(
                    round(float(free_mb) / 1024.0, 1) if free_mb is not None else None
                ),
            )
    except Exception:  # noqa: BLE001 — GPUtil missing, nvidia-smi absent, parse error
        logger.debug("gpu_probe_nvidia_unavailable", exc_info=True)

    if _platform.system() == "Darwin" and _platform.machine() == "arm64":
        try:
            import psutil

            available = psutil.virtual_memory().available
            return Capacity(vram_available_gb=round(available / (1024 ** 3), 1))
        except Exception:  # noqa: BLE001
            logger.debug("gpu_probe_apple_unavailable", exc_info=True)

    return Capacity()


# Keys carried over from the sidecar's detect_hardware() output. Static facts
# the coordinator's tier score reads (recommended_tier, gpu_name, gpu_vram_gb)
# plus the shape of the machine. Deliberately excluded: ram_available_gb
# (dynamic, belongs in heartbeats) and cpu_name (fingerprinting surface with
# no routing value).
_DETECTED_KEYS = (
    "os",
    "arch",
    "cpu_cores_physical",
    "cpu_cores_logical",
    "ram_total_gb",
    "gpu_name",
    "gpu_vram_gb",
    "recommended_tier",
)


def legacy_hardware_summary() -> dict[str, object]:
    """The four keys every contributor has always sent. Kept verbatim so the
    coordinator's fleet view (groups by ``system``) and the tier score's
    fallback path keep working for a peer whose detection fails."""
    import os

    return {
        "system": _platform.system(),
        "release": _platform.release(),
        "machine": _platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def full_hardware_summary(detected: dict[str, object] | None = None) -> dict[str, object]:
    """Legacy keys plus the detected hardware facts.

    ``detected`` is the sidecar's ``detect_hardware()`` dict when the caller
    already has it cached; when None it is detected here (this shells out to
    system tools once, so call it off the event loop or pass the cache).
    Detection failure degrades to the legacy summary, never to an exception.
    """
    summary = legacy_hardware_summary()
    if detected is None:
        try:
            from potluck_local.services.hardware_detect import detect_hardware

            detected = detect_hardware()
        except Exception:  # noqa: BLE001
            logger.debug("hardware_detect_failed", exc_info=True)
            return summary
    for key in _DETECTED_KEYS:
        if key in detected and detected[key] is not None:
            summary[key] = detected[key]
    return summary
