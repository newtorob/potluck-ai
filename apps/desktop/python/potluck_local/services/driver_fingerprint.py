"""A short string that changes when the graphics driver does.

The answer check (acceptance.py) trusts a result only for the setup it ran on,
and a driver update is the change that fixes or breaks a GPU. Reading it must be
cheap and must never stop a load, so every reader here works without a child
process and the public function never raises.

  Windows  DriverVersion and DriverDate of the display adapter, from the same
           class key windows_gpu.registry_adapter_memory walks, matched to the
           device by name.
  Linux    /proc/driver/nvidia/version when it exists. Otherwise the kernel
           driver bound to a display card (amdgpu, i915, xe) with its module
           version when the module publishes one, else the kernel release.
           Otherwise the kernel release alone.
  macOS    The OS version and build. The GPU driver ships inside the OS.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
from pathlib import Path

from potluck_local.services import windows_gpu

logger = logging.getLogger(__name__)

_MAX_LENGTH = 120
_NVIDIA_VERSION_FILE = Path("/proc/driver/nvidia/version")
_DRM_DIR = Path("/sys/class/drm")
_MODULE_DIR = Path("/sys/module")
# Kernel drivers that run a display card, by the vendor words in a device name.
_VENDOR_DRIVERS = (
    (("nvidia", "geforce", "quadro", "tesla", "rtx", "gtx"), ("nvidia",)),
    (("amd", "radeon", "ati"), ("amdgpu", "radeon")),
    (("intel", "arc", "uhd", "iris"), ("i915", "xe")),
)
_DISPLAY_DRIVERS = ("nvidia", "amdgpu", "radeon", "i915", "xe", "nouveau")


def driver_fingerprint(device_name: str | None = None) -> str | None:
    """The driver of *device_name* (or of the machine's display adapters when no
    name is given), as a short string. None when it cannot be read."""
    try:
        if sys.platform == "win32":
            text = _windows(device_name)
        elif sys.platform.startswith("linux"):
            text = _linux(device_name)
        elif sys.platform == "darwin":
            text = _macos()
        else:
            text = None
    except Exception as exc:  # noqa: BLE001 - a fingerprint must never stop a load
        logger.debug("driver_fingerprint failed: %s", type(exc).__name__)
        return None
    text = " ".join((text or "").split())[:_MAX_LENGTH]
    return text or None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _windows(device_name: str | None) -> str | None:
    adapters = _registry_drivers()
    if device_name:
        # Cards with one name normally share a driver; if they do not, or the
        # name on the two sides disagrees, nothing is guessed.
        drivers = {a["driver"] for a in adapters if windows_gpu.names_match(device_name, a["name"])}
        return drivers.pop() if len(drivers) == 1 else None
    hardware = [a for a in adapters if "basic" not in a["name"].lower()] or adapters
    return "; ".join(sorted({a["driver"] for a in hardware})) or None


def _registry_drivers() -> list[dict]:
    """Display adapters from the driver class key: name and "version (date)"."""
    try:
        import winreg
    except ImportError:
        return []
    found: list[dict] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, windows_gpu._DISPLAY_CLASS_KEY) as class_key:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(class_key, index)
                except OSError:
                    break
                index += 1
                if not sub.isdigit():
                    continue  # "Configuration", "Properties"
                try:
                    with winreg.OpenKey(class_key, sub) as adapter_key:
                        def value(name: str, _key=adapter_key) -> object:
                            try:
                                return winreg.QueryValueEx(_key, name)[0]
                            except OSError:
                                return None

                        name = windows_gpu._registry_text(value("DriverDesc")) or windows_gpu._registry_text(
                            value("HardwareInformation.AdapterString")
                        )
                        version = windows_gpu._registry_text(value("DriverVersion"))
                        date = windows_gpu._registry_text(value("DriverDate"))
                except OSError:
                    continue
                if name and version:
                    found.append({"name": name, "driver": f"{version} ({date})" if date else version})
    except OSError as exc:
        logger.debug("driver_fingerprint: display class key unreadable: %s", exc)
        return []
    return found


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _linux(device_name: str | None) -> str | None:
    wanted = _drivers_for(device_name)  # () when the name says nothing about the vendor
    release = platform.release()
    parts: list[str] = []
    if not wanted or "nvidia" in wanted:
        nvidia = _nvidia_version()
        if nvidia:
            parts.append(nvidia)
    for driver in _bound_display_drivers():
        if (wanted and driver not in wanted) or (driver == "nvidia" and parts):
            continue
        version = _read(_MODULE_DIR / driver / "version")
        parts.append(f"{driver} {version}" if version else f"{driver} (kernel {release})")
    return "; ".join(parts) or (f"kernel {release}" if release else None)


def _drivers_for(device_name: str | None) -> tuple[str, ...]:
    lowered = (device_name or "").lower()
    for words, drivers in _VENDOR_DRIVERS:
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            return drivers
    return ()


def _nvidia_version() -> str | None:
    text = _read(_NVIDIA_VERSION_FILE)
    if not text:
        return None
    match = re.search(r"Kernel Module\s+([0-9][0-9A-Za-z._-]*)", text)
    if match:
        return f"nvidia {match.group(1)}"
    return text.splitlines()[0]


def _bound_display_drivers() -> list[str]:
    """Kernel drivers bound to the cards in /sys/class/drm, in card order."""
    found: list[str] = []
    try:
        cards = sorted(p for p in _DRM_DIR.iterdir() if re.fullmatch(r"card\d+", p.name))
    except OSError:
        return found
    for card in cards:
        try:
            driver = Path(os.readlink(card / "device" / "driver")).name
        except OSError:
            continue
        if driver in _DISPLAY_DRIVERS and driver not in found:
            found.append(driver)
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _macos() -> str | None:
    version = platform.mac_ver()[0]
    build = _sysctl_string("kern.osversion")
    if version and build:
        return f"macOS {version} ({build})"
    return f"macOS {version}" if version else None


def _sysctl_string(name: str) -> str | None:
    """A string sysctl, read through libc so no child process is needed."""
    import ctypes

    libc = ctypes.CDLL(None)
    size = ctypes.c_size_t(0)
    if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(name.encode(), buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.value.decode("utf-8", "replace").strip() or None
