"""Windows GPU memory facts that ggml does not report: what every process is
using, which adapter is which, and the real dedicated size of a large card.

Why this module exists
----------------------
``ggml_backend_dev_memory`` reports "free" memory from the Vulkan driver. On an
AMD RX 6600 XT (2026-09-13 and 2026-10-09) that figure tracked only what the
engine itself had allocated: it was the same with a game holding 1.4 GiB of the
card and with the machine idle. A load planned from it filled a card that other
programs were already using, and every inference afterwards failed with an
out-of-device-memory error.

Windows itself keeps the system-wide figure. The WDDM video memory manager
publishes it as the performance counter set ``GPU Adapter Memory`` (one
instance per adapter, named by LUID), the same source Task Manager draws its
"Dedicated GPU memory" graph from. This module reads it two ways at once:

* **PDH** (``pdh.dll``) supplies ``Dedicated Usage``: bytes in use on each
  adapter by all processes, right now. ctypes only, no child process.
* **DXGI** (``dxgi.dll``) supplies the identity PDH lacks: each adapter's LUID,
  name and dedicated size, so a usage figure can be matched to the device the
  engine is offloading to.

``IDXGIAdapter3::QueryVideoMemoryInfo`` is deliberately not used for usage. Its
``CurrentUsage`` is the calling process's own use, and its ``Budget`` is an
allowance the OS hands this process; neither says how much the other programs
on the machine are holding. Read on the RX 6600 XT machine on 2026-10-09 (previous
display driver) from a fresh process, with 4.55 GiB of the card in use by others:
``CurrentUsage`` 0.00 GiB and ``Budget`` 7.21 GiB, against a card of 7.96 GiB.

The registry value ``HardwareInformation.qwMemorySize`` (64-bit) is read for
hardware detection, because ``Win32_VideoController.AdapterRAM`` is a 32-bit
field that tops out at 4 GiB.

Everything here is Windows-only and imports cleanly elsewhere: ctypes and
winreg are touched inside functions that check the platform first. Every public
reader returns a result describing what went wrong instead of raising.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Performance counter path, in English. PdhAddEnglishCounterW resolves it on any
# display language, so a localised Windows needs no special case.
_ADAPTER_DEDICATED_USAGE = r"\GPU Adapter Memory(*)\Dedicated Usage"
# The same figure per process. Only this engine's own share is taken from it,
# so a model still being released is not counted as another program's use.
_PROCESS_DEDICATED_USAGE = r"\GPU Process Memory(*)\Dedicated Usage"

# PDH status codes (winerror.h / pdhmsg.h).
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_NO_OBJECT = 0xC0000BB8
_PDH_CSTATUS_NO_COUNTER = 0xC0000BB9
_PDH_FMT_LARGE = 0x00000400

_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 2

_INSTANCE_RE = re.compile(r"luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)_phys_(\d+)")
_PROCESS_INSTANCE_RE = re.compile(r"pid_(\d+)_luid_0x([0-9a-fA-F]+)_0x([0-9a-fA-F]+)_phys_(\d+)")

# Display adapters. Each numbered subkey is one driver instance.
_DISPLAY_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


class WindowsGpuUnavailable(Exception):
    """A reader could not produce a figure. The message says why, in words that
    are safe to put in a load report."""


@dataclass(frozen=True)
class AdapterMemory:
    """One display adapter as Windows sees it."""

    luid: str  # "0x00000000_0x0000d1e4": high and low parts, lowercase hex
    description: str | None  # DXGI adapter name; None when DXGI was unreadable
    dedicated_total_bytes: int | None  # DXGI DedicatedVideoMemory
    dedicated_used_bytes: int  # PDH, summed over every process on this adapter
    own_used_bytes: int = 0  # the part of that held by this process (0 if unreadable)
    software: bool = False  # Microsoft Basic Render Driver and the like

    def as_dict(self) -> dict:
        return {
            "luid": self.luid,
            "description": self.description,
            "dedicated_total_bytes": self.dedicated_total_bytes,
            "dedicated_used_bytes": self.dedicated_used_bytes,
            "own_used_bytes": self.own_used_bytes,
            "software": self.software,
        }


@dataclass(frozen=True)
class AdapterReading:
    """Result of one read of the system-wide figures."""

    status: str  # "ok", "unavailable", or "not_applicable" (not Windows)
    adapters: tuple[AdapterMemory, ...] = ()
    source: str = ""
    detail: str = ""


def names_match(a: str | None, b: str | None) -> bool:
    """Whether two adapter names are the same card, ignoring case and punctuation.

    Vulkan, DXGI, WMI and the registry all take the name from the driver, so
    they agree for nearly every card. Names are compared whole: "RX 6600" and
    "RX 6600 XT" are different cards, and a prefix rule would join them. A card
    whose spelling differs between APIs is matched by its dedicated size instead.
    """

    def key(name: str | None) -> str:
        # "Intel(R) Arc(TM)" and "Intel Arc" are the same card.
        return re.sub(r"[^a-z0-9]", "", re.sub(r"\((r|tm)\)", "", (name or "").lower()))

    ka, kb = key(a), key(b)
    return bool(ka) and ka == kb


def _luid_key(high: int, low: int) -> str:
    return f"0x{high & 0xFFFFFFFF:08x}_0x{low & 0xFFFFFFFF:08x}"


# ---------------------------------------------------------------------------
# PDH: bytes in use on each adapter, by everyone
# ---------------------------------------------------------------------------


def _pdh_read(paths: list[str]) -> list[list[tuple[str, int]]]:
    """Read wildcard counters from one PDH query: ``[(instance, value), ...]`` per path.

    The values are instantaneous (these counters are not rates), so one
    collection is enough. Raises WindowsGpuUnavailable with the reason.
    """
    import ctypes

    class _Item(ctypes.Structure):
        # PDH_FMT_COUNTERVALUE_ITEM_W: name pointer, then {DWORD status; union}.
        _fields_ = [
            ("szName", ctypes.c_wchar_p),
            ("CStatus", ctypes.c_uint32),
            ("largeValue", ctypes.c_int64),
        ]

    try:
        pdh = ctypes.WinDLL("pdh")
    except OSError as exc:
        raise WindowsGpuUnavailable(f"pdh.dll could not be loaded ({exc})") from exc

    status_t = ctypes.c_long
    pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
    pdh.PdhOpenQueryW.restype = status_t
    pdh.PdhAddEnglishCounterW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p),
    ]
    pdh.PdhAddEnglishCounterW.restype = status_t
    pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    pdh.PdhCollectQueryData.restype = status_t
    pdh.PdhGetFormattedCounterArrayW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    pdh.PdhGetFormattedCounterArrayW.restype = status_t
    pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    pdh.PdhCloseQuery.restype = status_t

    def code(status: int) -> int:
        return status & 0xFFFFFFFF

    query = ctypes.c_void_p()
    status = code(pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)))
    if status != 0:
        raise WindowsGpuUnavailable(f"PdhOpenQuery failed (0x{status:08x})")
    try:
        counters = []
        for path in paths:
            counter = ctypes.c_void_p()
            status = code(pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(counter)))
            if status in (_PDH_CSTATUS_NO_OBJECT, _PDH_CSTATUS_NO_COUNTER):
                raise WindowsGpuUnavailable("this Windows has no GPU memory performance counters")
            if status != 0:
                raise WindowsGpuUnavailable(f"PdhAddCounter failed (0x{status:08x})")
            counters.append(counter)
        status = code(pdh.PdhCollectQueryData(query))
        if status != 0:
            raise WindowsGpuUnavailable(f"PdhCollectQueryData failed (0x{status:08x})")

        results: list[list[tuple[str, int]]] = []
        for counter in counters:
            size = ctypes.c_uint32(0)
            count = ctypes.c_uint32(0)
            status = code(
                pdh.PdhGetFormattedCounterArrayW(
                    counter, _PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None
                )
            )
            if status != _PDH_MORE_DATA:
                raise WindowsGpuUnavailable(f"PdhGetFormattedCounterArray sizing failed (0x{status:08x})")
            buffer = ctypes.create_string_buffer(size.value)
            status = code(
                pdh.PdhGetFormattedCounterArrayW(
                    counter, _PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), buffer
                )
            )
            if status != 0:
                raise WindowsGpuUnavailable(f"PdhGetFormattedCounterArray failed (0x{status:08x})")
            rows: list[tuple[str, int]] = []
            for item in (_Item * count.value).from_buffer(buffer):
                if item.CStatus in (0, 1):  # PDH_CSTATUS_VALID_DATA, NEW_DATA
                    rows.append((item.szName or "", max(0, int(item.largeValue))))
            results.append(rows)
        return results
    finally:
        pdh.PdhCloseQuery(query)


def pdh_dedicated_usage(pid: int | None = None) -> tuple[dict[str, int], dict[str, int]]:
    """Dedicated GPU memory in use, by adapter LUID: ``(everyone, this process)``.

    The first dict is the adapter counter, which counts every process on the
    machine. The second is the share held by process *pid* (default: this
    one), taken from the per-process counter; it is empty when that counter
    cannot be read, which only means the caller subtracts nothing.

    Raises WindowsGpuUnavailable with the reason when the adapter counters
    cannot be read (Windows older than 10 1709, a stripped image, a locked-down
    performance counter service).
    """
    if sys.platform != "win32":
        raise WindowsGpuUnavailable("not Windows")
    import os

    own_pid = os.getpid() if pid is None else pid

    adapter_rows = _pdh_read([_ADAPTER_DEDICATED_USAGE])[0]
    everyone: dict[str, int] = {}
    for name, value in adapter_rows:
        match = _INSTANCE_RE.search(name)
        if match:
            key = _luid_key(int(match.group(1), 16), int(match.group(2), 16))
            everyone[key] = everyone.get(key, 0) + value
    if not everyone:
        raise WindowsGpuUnavailable("the GPU Adapter Memory counters listed no adapters")

    own: dict[str, int] = {}
    try:
        for name, value in _pdh_read([_PROCESS_DEDICATED_USAGE])[0]:
            match = _PROCESS_INSTANCE_RE.search(name)
            if match and int(match.group(1)) == own_pid:
                key = _luid_key(int(match.group(2), 16), int(match.group(3), 16))
                own[key] = own.get(key, 0) + value
    except WindowsGpuUnavailable as exc:
        logger.debug("windows_gpu: per-process counter unavailable: %s", exc)
    return everyone, own


# ---------------------------------------------------------------------------
# DXGI: which adapter is which
# ---------------------------------------------------------------------------


def dxgi_adapters() -> list[dict]:
    """Adapters from DXGI: LUID, name and dedicated size. Raises WindowsGpuUnavailable."""
    if sys.platform != "win32":
        raise WindowsGpuUnavailable("not Windows")
    import ctypes

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
        ]

    class _Luid(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

    class _Desc1(ctypes.Structure):
        # DXGI_ADAPTER_DESC1. SIZE_T is 64-bit in a 64-bit process, which is why
        # DedicatedVideoMemory is right for cards over 4 GiB.
        _fields_ = [
            ("Description", ctypes.c_wchar * 128),
            ("VendorId", ctypes.c_uint32), ("DeviceId", ctypes.c_uint32),
            ("SubSysId", ctypes.c_uint32), ("Revision", ctypes.c_uint32),
            ("DedicatedVideoMemory", ctypes.c_size_t),
            ("DedicatedSystemMemory", ctypes.c_size_t),
            ("SharedSystemMemory", ctypes.c_size_t),
            ("AdapterLuid", _Luid), ("Flags", ctypes.c_uint32),
        ]

    # IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
    iid_factory1 = _Guid(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))

    def method(obj: int, index: int, restype, *argtypes):  # noqa: ANN001
        """Call slot *index* of a COM object's vtable."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
        slots = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(slots[index])

    def release(obj: int) -> None:
        method(obj, 2, ctypes.c_ulong)(obj)

    try:
        dxgi = ctypes.WinDLL("dxgi")
        create = dxgi.CreateDXGIFactory1
    except (OSError, AttributeError) as exc:
        raise WindowsGpuUnavailable(f"dxgi.dll could not be loaded ({exc})") from exc
    create.argtypes = [ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long

    factory = ctypes.c_void_p()
    hr = create(ctypes.byref(iid_factory1), ctypes.byref(factory)) & 0xFFFFFFFF
    if hr != 0 or not factory.value:
        raise WindowsGpuUnavailable(f"CreateDXGIFactory1 failed (0x{hr:08x})")

    adapters: list[dict] = []
    try:
        # IDXGIFactory1::EnumAdapters1 is slot 12, IDXGIAdapter1::GetDesc1 slot 10.
        enum_adapters1 = method(factory.value, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        for index in range(32):
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory.value, index, ctypes.byref(adapter)) & 0xFFFFFFFF
            if hr == _DXGI_ERROR_NOT_FOUND:
                break
            if hr != 0 or not adapter.value:
                raise WindowsGpuUnavailable(f"EnumAdapters1 failed (0x{hr:08x})")
            try:
                desc = _Desc1()
                get_desc1 = method(adapter.value, 10, ctypes.c_long, ctypes.POINTER(_Desc1))
                hr = get_desc1(adapter.value, ctypes.byref(desc)) & 0xFFFFFFFF
                if hr != 0:
                    raise WindowsGpuUnavailable(f"GetDesc1 failed (0x{hr:08x})")
                adapters.append(
                    {
                        "luid": _luid_key(desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart),
                        "description": desc.Description.strip(),
                        "dedicated_total_bytes": int(desc.DedicatedVideoMemory),
                        "software": bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE),
                    }
                )
            finally:
                release(adapter.value)
    finally:
        release(factory.value)
    return adapters


# ---------------------------------------------------------------------------
# The reader the engine calls
# ---------------------------------------------------------------------------


def read_adapter_memory() -> AdapterReading:
    """Read system-wide dedicated GPU memory use for every adapter. Never raises."""
    if sys.platform != "win32":
        return AdapterReading("not_applicable", detail="not Windows")
    try:
        usage, own_usage = pdh_dedicated_usage()
    except WindowsGpuUnavailable as exc:
        return AdapterReading("unavailable", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - a diagnostics reader must not raise
        return AdapterReading("unavailable", detail=f"performance counters failed ({type(exc).__name__})")

    try:
        identities = {a["luid"]: a for a in dxgi_adapters()}
        source = "pdh+dxgi"
    except WindowsGpuUnavailable as exc:
        logger.debug("windows_gpu: DXGI unavailable: %s", exc)
        identities, source = {}, "pdh"
    except Exception as exc:  # noqa: BLE001
        logger.debug("windows_gpu: DXGI failed: %s", type(exc).__name__)
        identities, source = {}, "pdh"

    adapters = []
    for luid, used in sorted(usage.items()):
        known = identities.get(luid) or {}
        adapters.append(
            AdapterMemory(
                luid=luid,
                description=known.get("description"),
                dedicated_total_bytes=known.get("dedicated_total_bytes"),
                dedicated_used_bytes=used,
                own_used_bytes=min(used, own_usage.get(luid, 0)),
                software=bool(known.get("software")),
            )
        )
    return AdapterReading("ok", tuple(adapters), source=source)


# ---------------------------------------------------------------------------
# Registry: the real dedicated size of the card
# ---------------------------------------------------------------------------


def _registry_text(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw.strip("\x00 ").strip() or None
    if isinstance(raw, (bytes, bytearray)):
        # Some drivers store the adapter string as REG_BINARY UTF-16.
        for encoding in ("utf-16-le", "utf-8"):
            try:
                text = bytes(raw).decode(encoding).strip("\x00 ").strip()
            except UnicodeDecodeError:
                continue
            if text:
                return text
    return None


def _registry_size(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, (bytes, bytearray)) and len(raw) in (4, 8):
        value = int.from_bytes(bytes(raw), "little")
    else:
        return None
    return value if value > 0 else None


def registry_adapter_memory() -> list[dict]:
    """Display adapters from the driver class key: ``{"name", "dedicated_bytes"}``.

    ``HardwareInformation.qwMemorySize`` is a 64-bit value written by the
    display driver, so an 8 GiB card reads 8 GiB where
    ``Win32_VideoController.AdapterRAM`` reads 4 GiB. Entries without it are
    left out. Returns an empty list when the key cannot be read.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    found: list[dict] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_KEY) as class_key:
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

                        size = _registry_size(value("HardwareInformation.qwMemorySize"))
                        name = _registry_text(value("DriverDesc")) or _registry_text(
                            value("HardwareInformation.AdapterString")
                        )
                except OSError:
                    continue
                if name and size:
                    found.append({"name": name, "dedicated_bytes": size})
    except OSError as exc:
        logger.debug("windows_gpu: display class key unreadable: %s", exc)
        return []
    return found
