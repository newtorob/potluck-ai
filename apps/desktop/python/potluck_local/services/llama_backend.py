"""Choose which llama.cpp backend this process loads, and report what it chose.

Background
----------
Until now the Windows bundle shipped whatever ``pip install llama-cpp-python``
produced, which is the CPU-only wheel: its ``lib/`` holds ``ggml-cpu.dll`` and
nothing else, and ``llama_supports_gpu_offload()`` returns false. Asking for
``n_gpu_layers=-1`` on that build cannot offload anything, because there is no
GPU backend in the bundle to offload to.

The fix ships two complete backend sets side by side:

    llama_cpp/lib          CPU only. The wheel default, present everywhere.
    llama_cpp/lib-vulkan   Vulkan. Same llama-cpp-python version, same
                           vendored llama.cpp build, so a CPU/GPU comparison
                           is a comparison of devices and not of versions.

``llama-cpp-python`` reads ``LLAMA_CPP_LIB_PATH`` at import time and loads
``llama.dll`` from there, so the choice has to be made before anything in the
process imports ``llama_cpp``.

Why a separate process does the probing
---------------------------------------
``ggml.dll`` in the Vulkan set links ``ggml-vulkan.dll``, which links
``vulkan-1.dll``. On a machine with no Vulkan driver (a VM on the basic display
adapter, a stripped Windows image) loading it fails, and a driver that faults
during device enumeration takes the whole process with it. The sidecar has to
start even when the GPU path is broken, so the probe runs in a child process
and a failure there is just a CPU selection.

Nothing in this module infers a device from installed hardware. A GPU is only
selected after ggml itself reported that it has one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from potluck_local.services import windows_gpu

logger = logging.getLogger(__name__)

# Backend override. "auto" probes, "cpu" forces the CPU set, "vulkan" forces the
# Vulkan set without probing (for testing a machine we cannot reach).
BACKEND_ENV = "POTLUCK_LLAMA_BACKEND"

# llama-cpp-python's own override, which we set once the choice is made.
LIB_PATH_ENV = "LLAMA_CPP_LIB_PATH"

# Directory name of each optional backend set inside the llama_cpp package.
_BACKEND_DIRS: dict[str, str] = {"vulkan": "lib-vulkan"}

# Preference order among GPU backends. One entry today; CUDA would slot in
# ahead of Vulkan for NVIDIA once there is NVIDIA hardware to verify it on.
_GPU_PREFERENCE: tuple[str, ...] = ("vulkan",)

_PROBE_TIMEOUT_SECONDS = 60.0
_CACHE_TTL_SECONDS = 7 * 24 * 3600

# ggml_backend_dev_type from the pinned ggml/include/ggml-backend.h.
# IGPU precedes ACCEL; reversing them hides Intel/AMD integrated graphics.
_DEV_TYPE = {0: "cpu", 1: "gpu", 2: "igpu", 3: "accel", 4: "meta"}

# Bytes held back when sizing an offload, in two named parts so a load report can
# say what each is for. A model that spills past the end of VRAM streams its
# weights over PCIe on every token, which is slower than staying on the CPU, so
# the total is deliberately generous.
#
# Engine overhead: what the engine allocates beyond the weights file plus the KV
# cache (compute buffers, driver allocations). Measured 2026-10-09 on an RX 6600
# XT with the Vulkan backend: qwen3-8b-q4 at n_ctx 8192 held 5.92 GiB on the card,
# against 5.81 GiB for weights (4.68 GiB file) plus KV cache (1.125 GiB), so the
# plan's two terms were 0.11 GiB low. The file also contains the input token
# embedding, which llama.cpp keeps in host memory (about 0.33 GiB for this
# model, computed from vocabulary and width), so the compute buffers are larger
# than 0.11 GiB and that table offsets them. Compute buffers grow with the
# context once flash attention is off: llama.cpp reserved 302 MiB at 8192 and
# 969 MiB at 32768 for a 14-head 0.5B model measured on the CPU backend on
# 2026-10-09. 256 MiB is 2.2x the measured shortfall; it is part of the reserve
# and not an addition to it, so a card with nothing else on it is planned
# exactly as before. Re-measure on Vulkan before shrinking it.
_ENGINE_OVERHEAD_BYTES = 256 * 1024 * 1024
# Room for the other programs on the card to grow while the model loads. A load
# took 175 s on the machine above, long enough for a game to load a level.
_SHARING_MARGIN_BYTES = 768 * 1024 * 1024
_VRAM_RESERVE_BYTES = _ENGINE_OVERHEAD_BYTES + _SHARING_MARGIN_BYTES  # 1 GiB, as before

# A DXGI dedicated size and ggml's device total describe the same card within
# this fraction (8,543,391,744 against 8,573,157,376 bytes on the RX 6600 XT).
_TOTAL_MATCH_TOLERANCE = 0.05

# Never shrink the context below this to make a GPU offload fit. Past this
# point the offload is buying speed the user did not ask to pay for in
# conversation length, and the CPU at a long context is the better trade.
_MIN_GPU_CTX = 8192


@dataclass
class Selection:
    """What backend this process is going to load, and why."""

    backend: str  # "cpu", "vulkan", or "bundled" when we left the wheel alone
    lib_dir: Path | None
    reason: str
    devices: list[dict] = field(default_factory=list)
    probed: bool = False

    @property
    def gpu_devices(self) -> list[dict]:
        return [d for d in self.devices if d.get("type") in ("gpu", "igpu")]

    def as_dict(self) -> dict:
        return {
            "backend": self.backend,
            "lib_dir": str(self.lib_dir) if self.lib_dir else None,
            "reason": self.reason,
            "probed": self.probed,
            "devices": self.devices,
        }


_selection: Selection | None = None


# ---------------------------------------------------------------------------
# Locating the backend sets
# ---------------------------------------------------------------------------


def llama_cpp_package_dir() -> Path | None:
    """Directory of the installed llama_cpp package, without importing it.

    Importing would load the native library, which is the decision we have not
    made yet.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("llama_cpp")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent


def available_backends() -> dict[str, Path]:
    """Optional backend sets present in this install, by name."""
    pkg = llama_cpp_package_dir()
    if pkg is None:
        return {}
    found: dict[str, Path] = {}
    for name, dirname in _BACKEND_DIRS.items():
        candidate = pkg / dirname
        # Linux wheels keep auditwheel's relative dependency layout inside
        # the backend set, isolated from the CPU wheel's hashed libraries.
        nested = candidate / "llama_cpp" / "lib"
        if nested.is_dir():
            candidate = nested
        if candidate.is_dir() and any(candidate.iterdir()):
            found[name] = candidate
    return found


# ---------------------------------------------------------------------------
# Asking ggml what devices it has
# ---------------------------------------------------------------------------


def describe_devices(lib_dir: Path | None = None) -> list[dict]:
    """Enumerate ggml's devices by calling into the shared library.

    This is the engine's own device list, not a guess from a hardware scan. It
    is safe to call after llama_cpp is already imported: loading the same
    library path again returns the module that is already resident.

    Returns an empty list if the library cannot be loaded or does not export
    the backend-registry symbols.
    """
    import ctypes

    try:
        import llama_cpp._ctypes_extensions as ext
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return []

    if lib_dir is None:
        pkg = llama_cpp_package_dir()
        if pkg is None:
            return []
        lib_dir = Path(os.environ.get(LIB_PATH_ENV) or (pkg / "lib"))

    # The registry (dev_count/dev_get) lives in ggml; the device interface
    # (name/description/type/memory) lives in ggml-base. Load both and resolve
    # each symbol from whichever exports it.
    libs = []
    for base in ("ggml", "ggml-base"):
        try:
            libs.append(ext.load_shared_library(base, Path(lib_dir)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("llama_backend: could not load %s from %s: %s", base, lib_dir, exc)
    if not libs:
        return []

    def sym(name: str, argtypes: list, restype):  # noqa: ANN001
        for lib in libs:
            try:
                fn = getattr(lib, name)
            except AttributeError:
                continue
            fn.argtypes = argtypes
            fn.restype = restype
            return fn
        return None

    size_p = ctypes.POINTER(ctypes.c_size_t)
    dev_count = sym("ggml_backend_dev_count", [], ctypes.c_size_t)
    dev_get = sym("ggml_backend_dev_get", [ctypes.c_size_t], ctypes.c_void_p)
    dev_name = sym("ggml_backend_dev_name", [ctypes.c_void_p], ctypes.c_char_p)
    dev_desc = sym("ggml_backend_dev_description", [ctypes.c_void_p], ctypes.c_char_p)
    dev_type = sym("ggml_backend_dev_type", [ctypes.c_void_p], ctypes.c_int)
    dev_mem = sym("ggml_backend_dev_memory", [ctypes.c_void_p, size_p, size_p], None)
    if not all((dev_count, dev_get, dev_name, dev_type, dev_mem)):
        logger.debug("llama_backend: ggml backend-registry symbols missing in %s", lib_dir)
        return []

    out: list[dict] = []
    try:
        for i in range(dev_count()):
            dev = dev_get(i)
            free = ctypes.c_size_t(0)
            total = ctypes.c_size_t(0)
            dev_mem(dev, ctypes.byref(free), ctypes.byref(total))
            out.append(
                {
                    "name": dev_name(dev).decode("utf-8", "replace"),
                    "description": (
                        dev_desc(dev).decode("utf-8", "replace") if dev_desc else "unknown"
                    ),
                    "type": _DEV_TYPE.get(dev_type(dev), "other"),
                    "free_bytes": free.value,
                    "total_bytes": total.value,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("llama_backend: device enumeration failed: %s", exc)
        return []
    return out


# ---------------------------------------------------------------------------
# The probe, as run in the child process
# ---------------------------------------------------------------------------


def probe_lib_dir(lib_dir: Path) -> dict:
    """Load a backend set and report what it can do. Runs in the child process.

    Never raises: a backend that cannot load is a result, not an error.
    """
    result: dict = {
        "lib_dir": str(lib_dir),
        "loaded": False,
        "supports_gpu_offload": False,
        "devices": [],
        "error": None,
    }
    os.environ[LIB_PATH_ENV] = str(lib_dir)
    try:
        import llama_cpp

        result["loaded"] = True
        result["wrapper_version"] = getattr(llama_cpp, "__version__", None)
        result["supports_gpu_offload"] = bool(llama_cpp.llama_supports_gpu_offload())
        result["devices"] = describe_devices(lib_dir)
    except BaseException as exc:  # noqa: BLE001 - report, do not propagate
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _probe_command(lib_dir: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--probe-inference-backend", str(lib_dir)]
    return [
        sys.executable,
        "-m",
        "potluck_local.app",
        "--probe-inference-backend",
        str(lib_dir),
    ]


def run_probe(lib_dir: Path, *, timeout: float = _PROBE_TIMEOUT_SECONDS) -> dict | None:
    """Probe a backend set in a child process. None when the child never answered."""
    env = dict(os.environ)
    env.pop(LIB_PATH_ENV, None)
    env["POTLUCK_PROBE_CHILD"] = "1"
    creationflags = 0
    if sys.platform == "win32":
        # Do not flash a console window when the packaged app spawns the probe.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            _probe_command(lib_dir),
            capture_output=True,
            timeout=timeout,
            env=env,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        logger.warning("llama_backend: probe of %s timed out after %.0fs", lib_dir, timeout)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("llama_backend: probe of %s could not start: %s", lib_dir, exc)
        return None

    stdout = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    if proc.returncode != 0:
        logger.warning("llama_backend: probe of %s failed with exit %s", lib_dir, proc.returncode)
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
                return result if isinstance(result, dict) else None
            except json.JSONDecodeError:
                continue
    logger.warning(
        "llama_backend: probe of %s exited %s with no result (stderr: %s)",
        lib_dir,
        proc.returncode,
        (proc.stderr or b"")[-400:].decode("utf-8", "replace").strip(),
    )
    return None


# ---------------------------------------------------------------------------
# Probe cache
# ---------------------------------------------------------------------------


def _signature(dirs: dict[str, Path]) -> str:
    """Fingerprint the backend sets so a rebuilt bundle re-probes."""
    parts = [platform.platform(), platform.machine()]
    for name in sorted(dirs):
        parts.append(name)
        for f in sorted(dirs[name].iterdir()):
            try:
                parts.append(f"{f.name}:{f.stat().st_size}")
            except OSError:
                parts.append(f"{f.name}:?")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _cache_path() -> Path:
    from potluck_local.utils.paths import data_dir

    return data_dir() / "data" / "inference-backend.json"


def _read_cache(signature: str) -> dict | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("signature") != signature:
        return None
    if time.time() - float(payload.get("probed_at", 0)) > _CACHE_TTL_SECONDS:
        return None
    return payload


def _write_cache(signature: str, backend: str, devices: list[dict]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "signature": signature,
                    "backend": backend,
                    "devices": devices,
                    "probed_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("llama_backend: could not write probe cache: %s", exc)


def invalidate_cache() -> None:
    """Drop the cached probe result, so the next start probes again."""
    try:
        _cache_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_backend(*, force_reprobe: bool = False) -> Selection:
    """Decide which backend set this process loads, and set LLAMA_CPP_LIB_PATH.

    Call once, before anything imports llama_cpp. Repeat calls return the
    decision already made.
    """
    global _selection
    if _selection is not None and not force_reprobe:
        return _selection

    override = (os.environ.get(BACKEND_ENV) or "auto").strip().lower()
    extras = available_backends()

    if not extras:
        # Metal and custom/developer installs may have only the wheel's set.
        _selection = Selection(
            backend="bundled",
            lib_dir=None,
            reason="no additional backend sets are installed; using the wheel default",
        )
        return _selection

    if override == "cpu":
        os.environ.pop(LIB_PATH_ENV, None)
        _selection = Selection(
            backend="cpu",
            lib_dir=None,
            reason=f"{BACKEND_ENV}=cpu",
        )
        return _selection

    if override in extras:
        lib_dir = extras[override]
        os.environ[LIB_PATH_ENV] = str(lib_dir)
        _selection = Selection(
            backend=override,
            lib_dir=lib_dir,
            reason=f"{BACKEND_ENV}={override}, selected without probing",
        )
        return _selection

    if override != "auto":
        logger.warning(
            "llama_backend: %s=%r is not one of %s; falling back to auto",
            BACKEND_ENV,
            override,
            ["auto", "cpu", *sorted(extras)],
        )

    signature = _signature(extras)
    # Linux drivers and render-device access can change between starts (or
    # a machine can move between a desktop and a headless session). A stale
    # successful cache must not let a broken driver crash the main process;
    # a stale CPU cache must not hide a newly installed driver for a week.
    if not force_reprobe and sys.platform != "linux":
        cached = _read_cache(signature)
        if cached is not None:
            backend = str(cached.get("backend", "cpu"))
            if backend in extras:
                os.environ[LIB_PATH_ENV] = str(extras[backend])
                _selection = Selection(
                    backend=backend,
                    lib_dir=extras[backend],
                    reason="cached probe result",
                    devices=list(cached.get("devices") or []),
                    probed=True,
                )
            else:
                os.environ.pop(LIB_PATH_ENV, None)
                _selection = Selection(
                    backend="cpu",
                    lib_dir=None,
                    reason="cached probe found no usable GPU backend",
                    devices=list(cached.get("devices") or []),
                    probed=True,
                )
            return _selection

    for name in _GPU_PREFERENCE:
        lib_dir = extras.get(name)
        if lib_dir is None:
            continue
        probe = run_probe(lib_dir)
        if probe is None:
            logger.info("llama_backend: %s probe returned nothing; staying on CPU", name)
            continue
        if probe.get("error"):
            logger.info("llama_backend: %s unavailable: %s", name, probe["error"])
            continue
        devices = list(probe.get("devices") or [])
        gpus = [d for d in devices if d.get("type") in ("gpu", "igpu")]
        if not probe.get("loaded") or not probe.get("supports_gpu_offload") or not gpus:
            logger.info(
                "llama_backend: %s loaded but reported no GPU device; staying on CPU", name
            )
            _write_cache(signature, "cpu", devices)
            continue
        os.environ[LIB_PATH_ENV] = str(lib_dir)
        _write_cache(signature, name, devices)
        _selection = Selection(
            backend=name,
            lib_dir=lib_dir,
            reason=f"probe found {len(gpus)} GPU device(s): "
            + ", ".join(d.get("description") or d.get("name", "?") for d in gpus),
            devices=devices,
            probed=True,
        )
        logger.info("llama_backend: selected %s (%s)", name, _selection.reason)
        return _selection

    _write_cache(signature, "cpu", [])
    os.environ.pop(LIB_PATH_ENV, None)
    _selection = Selection(
        backend="cpu",
        lib_dir=None,
        reason="no GPU backend reported a usable device",
        probed=True,
    )
    return _selection


def current_selection() -> Selection:
    """The decision made earlier in this process, selecting now if needed."""
    return select_backend()


def reset_for_tests() -> None:
    """Forget the process-wide decision. Tests only."""
    global _selection
    _selection = None


# ---------------------------------------------------------------------------
# Sizing the offload
# ---------------------------------------------------------------------------


# GGUF metadata keys, by suffix, needed to size a load before it happens.
# llama.cpp knows all of this, but only after the model is loaded, which is the
# decision being made here.
_GGUF_WANTED = (
    ".block_count",
    ".attention.head_count_kv",
    ".attention.key_length",
    ".attention.value_length",
    ".attention.head_count",
    ".embedding_length",
)


def read_gguf_model_info(path: str | Path) -> dict[str, int]:
    """Read the numeric header fields we need, keyed by suffix without the dot.

    Returns whatever was found; an empty dict when the file is not readable
    GGUF. Never raises.
    """
    found: dict[str, int] = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"GGUF":
                return found
            (version,) = struct.unpack("<I", fh.read(4))
            if version not in (2, 3):
                return found
            fh.read(8)  # tensor_count
            (kv_count,) = struct.unpack("<Q", fh.read(8))
            if kv_count > 100_000:
                return found

            def read_str() -> str:
                (n,) = struct.unpack("<Q", fh.read(8))
                if n > 1 << 20:
                    raise ValueError("implausible GGUF string length")
                return fh.read(n).decode("utf-8", "replace")

            fixed = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

            def skip_value(vtype: int) -> None:
                if vtype in fixed:
                    fh.read(fixed[vtype])
                elif vtype == 8:
                    read_str()
                elif vtype == 9:
                    (elem_type,) = struct.unpack("<I", fh.read(4))
                    (count,) = struct.unpack("<Q", fh.read(8))
                    if elem_type in fixed:
                        fh.seek(fixed[elem_type] * count, os.SEEK_CUR)
                    elif elem_type == 8:
                        for _ in range(count):
                            read_str()
                    else:
                        raise ValueError(f"unknown GGUF array element type {elem_type}")
                else:
                    raise ValueError(f"unknown GGUF value type {vtype}")

            for _ in range(kv_count):
                key = read_str()
                (vtype,) = struct.unpack("<I", fh.read(4))
                suffix = next((w for w in _GGUF_WANTED if key.endswith(w)), None)
                if suffix is not None and vtype in (4, 5, 10, 11):
                    fmt = {4: "<I", 5: "<i", 10: "<Q", 11: "<q"}[vtype]
                    (value,) = struct.unpack(fmt, fh.read(fixed[vtype]))
                    if 0 < value < 10_000_000:
                        found[suffix.lstrip(".")] = int(value)
                    continue
                skip_value(vtype)
    except (OSError, struct.error, ValueError, UnicodeDecodeError):
        return found
    return found


def read_gguf_layer_count(path: str | Path) -> int | None:
    """Transformer block count, or None when the header is unreadable."""
    value = read_gguf_model_info(path).get("block_count")
    return value if value and 0 < value < 10_000 else None


def kv_cache_bytes(info: dict[str, int], n_ctx: int) -> int | None:
    """Bytes the KV cache needs for *n_ctx* tokens, or None when unknowable.

    Mirrors llama.cpp's own arithmetic: every layer holds a key and a value
    entry per token, per KV head, at 2 bytes each for the f16 default. This is
    the term the first version of the offload plan missed. On an 8 GB card, a
    2.3 GB model at a 131072-token context asks for 18 GB of KV cache, and the
    driver quietly backs the overflow with host memory.
    """
    layers = info.get("block_count")
    kv_heads = info.get("attention.head_count_kv")
    if not layers or not kv_heads or n_ctx <= 0:
        return None

    key_len = info.get("attention.key_length")
    value_len = info.get("attention.value_length")
    if not key_len or not value_len:
        # Older headers omit both and imply embedding_length / head_count.
        embedding = info.get("embedding_length")
        heads = info.get("attention.head_count")
        if not embedding or not heads:
            return None
        key_len = value_len = embedding // heads
        if key_len <= 0:
            return None

    return layers * kv_heads * (key_len + value_len) * 2 * n_ctx


@dataclass
class OffloadPlan:
    n_gpu_layers: int
    reason: str
    n_ctx: int | None = None
    requested_n_ctx: int | None = None
    model_bytes: int | None = None
    kv_bytes: int | None = None
    total_layers: int | None = None
    # The figure the plan was sized against: ggml's free figure, lowered to what
    # Windows says is free system-wide when that is smaller and could be read.
    free_vram_bytes: int | None = None
    # What ggml reported as free, before the system-wide figure was applied.
    reported_free_vram_bytes: int | None = None
    # Dedicated memory in use by programs other than this engine, when known.
    other_programs_vram_bytes: int | None = None
    vram_total_bytes: int | None = None
    reserve_bytes: int | None = None
    # free_vram_bytes less the reserve: what weights plus KV cache had to fit in.
    budget_bytes: int | None = None
    # The system-wide reading and how it was matched to this device (see SystemGpuUse).
    system_use: dict | None = None
    # One plain sentence about the memory check, for the load report and the log.
    memory_note: str | None = None

    def as_dict(self) -> dict:
        return {
            "n_gpu_layers": self.n_gpu_layers,
            "reason": self.reason,
            "n_ctx": self.n_ctx,
            "requested_n_ctx": self.requested_n_ctx,
            "model_bytes": self.model_bytes,
            "kv_bytes": self.kv_bytes,
            "total_layers": self.total_layers,
            "free_vram_bytes": self.free_vram_bytes,
            "reported_free_vram_bytes": self.reported_free_vram_bytes,
            "other_programs_vram_bytes": self.other_programs_vram_bytes,
            "vram_total_bytes": self.vram_total_bytes,
            "reserve_bytes": self.reserve_bytes,
            "budget_bytes": self.budget_bytes,
            "system_use": self.system_use,
            "memory_note": self.memory_note,
        }


def _round_down_ctx(n: int) -> int:
    """Contexts are conventionally multiples of 4096. Keep it tidy."""
    return (n // 4096) * 4096


def _gb(n: int | float) -> str:
    """Bytes as the GB figure Windows and this app show (binary, one decimal)."""
    return f"{n / 1024 ** 3:.1f} GB"


def offload_device(devices: list[dict]) -> tuple[int, dict] | None:
    """Prefer a dedicated GPU; integrated graphics is the fallback.

    The index is among ggml's GPU devices, matching llama.cpp's main_gpu.
    Do not size an NVIDIA load against an Intel iGPU's shared system RAM.
    """
    gpus = [d for d in devices if d.get("type") in ("gpu", "igpu")]
    candidates = [(i, d) for i, d in enumerate(gpus) if d.get("total_bytes")]
    dedicated = [(i, d) for i, d in candidates if d.get("type") == "gpu"]
    return max(dedicated or candidates, key=lambda item: int(item[1].get("free_bytes") or 0), default=None)


# ---------------------------------------------------------------------------
# What the rest of the machine is using (Windows)
# ---------------------------------------------------------------------------


@dataclass
class SystemGpuUse:
    """System-wide dedicated memory use on the adapter the plan offloads to.

    ``status`` is "ok" (figures below are set), "unavailable" (could not be
    read or matched; ``detail`` says why, and the plan then behaves as it did
    before this reading existed) or "not_applicable" (not Windows).
    """

    status: str
    detail: str = ""
    adapter: str | None = None
    luid: str | None = None
    # How the Windows adapter was matched to ggml's device: "description",
    # "description+dedicated_size", "dedicated_size", "only_adapter" or
    # "largest_dedicated_total", with "_most_used" added when several adapters
    # fit equally and the busiest was assumed.
    matched_by: str | None = None
    source: str = ""
    total_bytes: int | None = None
    used_bytes: int | None = None  # every program, this engine included
    own_bytes: int = 0  # this engine's share of used_bytes
    other_programs_bytes: int | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "adapter": self.adapter,
            "luid": self.luid,
            "matched_by": self.matched_by,
            "source": self.source,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "own_bytes": self.own_bytes,
            "other_programs_bytes": self.other_programs_bytes,
        }


def read_system_gpu_memory():  # -> windows_gpu.AdapterReading
    """The one place the system-wide GPU memory figures are read.

    Tests replace this function. On anything but Windows the reading is
    "not_applicable" and nothing is probed.
    """
    return windows_gpu.read_adapter_memory()


def _match_adapter(device: dict, adapters: list) -> tuple[object | None, str, str]:
    """Pick the Windows adapter that is ggml's *device*: ``(adapter, matched_by, why_not)``.

    ggml does not expose a LUID, so the match uses what both sides know: the
    adapter name, then the dedicated size, then (when neither separates them)
    the largest dedicated card. When several adapters fit equally, the busiest
    is assumed, because under-counting the load on a card is the costly error.
    """
    hardware = [a for a in adapters if not a.software]
    if not hardware:
        return None, "", "Windows lists no hardware display adapter"
    ggml_total = int(device.get("total_bytes") or 0)

    def size_fits(a) -> bool:  # noqa: ANN001
        return bool(
            a.dedicated_total_bytes
            and ggml_total
            and abs(a.dedicated_total_bytes - ggml_total) <= _TOTAL_MATCH_TOLERANCE * ggml_total
        )

    def busiest(group: list):
        return max(group, key=lambda a: a.dedicated_used_bytes)

    named = [a for a in hardware if windows_gpu.names_match(device.get("description"), a.description)]
    if len(named) == 1:
        return named[0], "description", ""
    if len(named) > 1:
        sized = [a for a in named if size_fits(a)]
        if len(sized) == 1:
            return sized[0], "description+dedicated_size", ""
        return busiest(sized or named), "description_most_used", ""

    sized = [a for a in hardware if size_fits(a)]
    if len(sized) == 1:
        return sized[0], "dedicated_size", ""
    if len(sized) > 1:
        return busiest(sized), "dedicated_size_most_used", ""

    if len(adapters) == 1:
        return adapters[0], "only_adapter", ""
    known = [a for a in hardware if a.dedicated_total_bytes]
    if known and len(known) == len(hardware):
        return max(known, key=lambda a: a.dedicated_total_bytes), "largest_dedicated_total", ""
    return None, "", "Windows could not say which of several adapters is the one being offloaded to"


def system_gpu_use(device: dict) -> SystemGpuUse:
    """What every program on the machine is using on *device*'s adapter. Never raises."""
    try:
        reading = read_system_gpu_memory()
    except Exception as exc:  # noqa: BLE001 - this must never stop a load
        return SystemGpuUse("unavailable", f"the reader failed ({type(exc).__name__})")
    if reading.status == "not_applicable":
        return SystemGpuUse("not_applicable", reading.detail)
    if reading.status != "ok":
        return SystemGpuUse("unavailable", reading.detail or "the reader gave no figure")
    if device.get("type") == "igpu":
        return SystemGpuUse(
            "unavailable",
            "the offload device is an integrated GPU, which shares system memory "
            "that the dedicated-memory counters do not describe",
            source=reading.source,
        )
    adapter, matched_by, why_not = _match_adapter(device, list(reading.adapters))
    if adapter is None:
        return SystemGpuUse("unavailable", why_not, source=reading.source)
    total = adapter.dedicated_total_bytes or int(device.get("total_bytes") or 0)
    if total <= 0:
        return SystemGpuUse("unavailable", "the adapter's dedicated size is unknown", source=reading.source)
    own = max(0, min(adapter.own_used_bytes, adapter.dedicated_used_bytes))
    return SystemGpuUse(
        "ok",
        adapter=adapter.description or device.get("description"),
        luid=adapter.luid,
        matched_by=matched_by,
        source=reading.source,
        total_bytes=total,
        used_bytes=adapter.dedicated_used_bytes,
        own_bytes=own,
        other_programs_bytes=adapter.dedicated_used_bytes - own,
    )


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def plan_gpu_layers(
    model_path: str | Path,
    *,
    requested: int,
    n_ctx: int,
    devices: list[dict] | None = None,
) -> OffloadPlan:
    """Decide how many layers go on the GPU, and at what context, for this model.

    ``requested >= 0`` is a user pin and is honoured unchanged. ``requested ==
    -1`` means "all layers, automatically", and this is where that becomes a
    real decision instead of a wish.

    Weights are not the whole cost. A 2.3 GB model at the auto-tuned context of
    131072 tokens asks for 18 GB of KV cache, which an 8 GB card cannot hold;
    the driver quietly backs the overflow with host memory and the first long
    conversation pays for it across PCIe. So the context is reduced first, the
    layer count second, and the CPU is the floor.

    What "free" means here. ``ggml_backend_dev_memory`` reports a figure that
    tracks what this process has allocated and nothing else: on an AMD RX 6600
    XT on Windows it was identical with a game holding 1.4 GiB of the card and
    with the machine idle (2026-09-13). It is close to the OS budget for the
    process: on 2026-10-09 DXGI's QueryVideoMemoryInfo read a budget of
    7.21 GiB with 4.55 GiB of the card in use, where ggml had reported 7.18 GiB
    free on the same card, so neither moves with the other programs' use. A plan
    sized from it filled a card other programs were using, and every inference
    afterwards failed. On Windows the plan is therefore sized against

        min(what ggml reports free, adapter total - dedicated memory in use by
            every other program) - reserve

    where the second term comes from Windows itself (windows_gpu.py). If that
    figure cannot be read, or cannot be matched to the offload device, the plan
    is sized from ggml's figure alone, as it was before, and the plan says so.
    A user on a busy card can still pin ``gpu_layers``.
    """
    if requested >= 0:
        return OffloadPlan(
            requested, "pinned by the gpu_layers setting", n_ctx=n_ctx, requested_n_ctx=n_ctx
        )

    if devices is None:
        devices = describe_devices()
    selected = offload_device(devices)
    if selected is None:
        return OffloadPlan(
            0,
            "no GPU device is available to this build",
            n_ctx=n_ctx,
            requested_n_ctx=n_ctx,
        )

    reported_free = int(selected[1].get("free_bytes") or 0)
    try:
        model_bytes = Path(model_path).stat().st_size
    except OSError:
        return OffloadPlan(
            -1,
            "model size unknown; asking for a full offload",
            n_ctx=n_ctx,
            requested_n_ctx=n_ctx,
        )

    system = system_gpu_use(selected[1])
    free = reported_free
    other_programs: int | None = None
    if system.status == "ok":
        other_programs = int(system.other_programs_bytes or 0)
        free = min(reported_free, max(0, int(system.total_bytes or 0) - other_programs))

    info = read_gguf_model_info(model_path)
    total_layers = info.get("block_count")
    kv_per_token = kv_cache_bytes(info, 1)

    def decide(free_bytes: int) -> tuple[int, str, int]:
        """(layers, reason, ctx) for this much free memory. The arithmetic is
        the same whatever produced the figure."""
        budget = free_bytes - _VRAM_RESERVE_BYTES
        if kv_per_token is None:
            # No KV arithmetic available: fall back to weights only, and say so.
            if budget >= model_bytes:
                return (
                    -1,
                    "the weights fit in free VRAM; the KV cache could not be sized "
                    "from this model's header",
                    n_ctx,
                )
            if not total_layers:
                return (
                    0,
                    "the model does not fit in free VRAM and its layer count is "
                    "unreadable, so it runs on the CPU",
                    n_ctx,
                )
            fits = max(0, min(total_layers, int(budget / (model_bytes / total_layers))))
            if fits == 0:
                return 0, "not enough free VRAM for even one layer, so it runs on the CPU", n_ctx
            return fits, f"{fits} of {total_layers} layers fit in free VRAM", n_ctx

        # Everything on the card at the requested context.
        if budget >= model_bytes + kv_per_token * n_ctx:
            return -1, "the model and its KV cache fit in free VRAM", n_ctx

        # Shrink the context until weights plus KV fit.
        room = budget - model_bytes
        if room > 0:
            fitted_ctx = _round_down_ctx(int(room / kv_per_token))
            if fitted_ctx >= _MIN_GPU_CTX:
                return (
                    -1,
                    f"context reduced from {n_ctx} to {fitted_ctx} so the KV cache fits in VRAM",
                    fitted_ctx,
                )

        # Context is already at its floor; give the GPU as many layers as it can hold.
        if total_layers:
            per_layer = model_bytes / total_layers + kv_per_token * _MIN_GPU_CTX / total_layers
            fits = max(0, min(total_layers, int(budget / per_layer))) if budget > 0 else 0
            if fits > 0:
                return (
                    fits,
                    f"{fits} of {total_layers} layers fit at an {_MIN_GPU_CTX}-token context",
                    _MIN_GPU_CTX,
                )

        return 0, "not enough free VRAM for a useful offload, so it runs on the CPU", n_ctx

    layers, reason, ctx = decide(free)

    memory_note: str | None = None
    if system.status == "ok":
        # Would the driver's figure alone have chosen differently? Then the
        # other programs are the reason, and the reason says so.
        alone = decide(reported_free)
        if (layers, ctx) != (alone[0], alone[2]):
            reason = f"{reason}, because {_gb(other_programs)} of the card is in use by other programs"
        memory_note = (
            f"{system.adapter} has {_gb(system.total_bytes)} and other programs are using "
            f"{_gb(other_programs)} of it; the driver reported {_gb(reported_free)} free. "
            f"The plan used {_gb(free)}, less {_gb(_VRAM_RESERVE_BYTES)} held back "
            f"({_gb(_ENGINE_OVERHEAD_BYTES)} for compute buffers and driver allocations, "
            f"{_gb(_SHARING_MARGIN_BYTES)} for other programs to grow)."
        )
    elif system.status == "unavailable":
        memory_note = (
            f"The system-wide figure for GPU memory used by other programs was unavailable "
            f"({system.detail}), so the plan used the driver's free-memory figure "
            f"of {_gb(reported_free)} only."
        )

    vram_total = system.total_bytes if system.status == "ok" else (int(selected[1].get("total_bytes") or 0) or None)
    return OffloadPlan(
        layers,
        reason,
        n_ctx=ctx,
        requested_n_ctx=n_ctx,
        model_bytes=model_bytes,
        kv_bytes=(kv_per_token * ctx) if kv_per_token else None,
        total_layers=total_layers,
        free_vram_bytes=free,
        reported_free_vram_bytes=reported_free,
        other_programs_vram_bytes=other_programs,
        vram_total_bytes=vram_total,
        reserve_bytes=_VRAM_RESERVE_BYTES,
        budget_bytes=free - _VRAM_RESERVE_BYTES,
        system_use=None if system.status == "not_applicable" else system.as_dict(),
        memory_note=memory_note,
    )
