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

# ggml_backend_dev_type, from ggml/include/ggml-backend.h.
_DEV_TYPE = {0: "cpu", 1: "gpu", 2: "accel", 3: "igpu"}

# Bytes held back for the KV cache and compute buffers. Deliberately generous:
# a model that spills past the end of VRAM streams its weights over PCIe on
# every token, which is slower than staying on the CPU.
_VRAM_RESERVE_BYTES = 1024 * 1024 * 1024

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
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
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
        # Every platform except the Windows bundle: one backend set, shipped by
        # the wheel (Metal on macOS, whatever the Linux build produced).
        _selection = Selection(
            backend="bundled",
            lib_dir=None,
            reason="no additional backend sets are installed; using the wheel default",
        )
        return _selection

    if override == "cpu":
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
    if not force_reprobe:
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
        if not probe.get("supports_gpu_offload") or not gpus:
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
    free_vram_bytes: int | None = None

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
        }


def _round_down_ctx(n: int) -> int:
    """Contexts are conventionally multiples of 4096. Keep it tidy."""
    return (n // 4096) * 4096


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

    What "free" means here, exactly: it is the figure ``ggml_backend_dev_memory``
    reports for the device. That tracks what this process has allocated, and on
    some drivers nothing more. Measured on an AMD RX 6600 XT on Windows
    (2026-09-13), the reported figure was identical with a game holding a large
    share of the card and with the machine idle, so it is not a live system-wide
    budget. A user on a busy card can pin ``gpu_layers`` instead, and the load
    report afterwards says where the weights actually went.
    """
    if requested >= 0:
        return OffloadPlan(
            requested, "pinned by the gpu_layers setting", n_ctx=n_ctx, requested_n_ctx=n_ctx
        )

    if devices is None:
        devices = describe_devices()
    gpus = [d for d in devices if d.get("type") in ("gpu", "igpu") and d.get("total_bytes")]
    if not gpus:
        return OffloadPlan(
            0,
            "no GPU device is available to this build",
            n_ctx=n_ctx,
            requested_n_ctx=n_ctx,
        )

    free = max(int(d.get("free_bytes") or 0) for d in gpus)
    try:
        model_bytes = Path(model_path).stat().st_size
    except OSError:
        return OffloadPlan(
            -1,
            "model size unknown; asking for a full offload",
            n_ctx=n_ctx,
            requested_n_ctx=n_ctx,
        )

    budget = free - _VRAM_RESERVE_BYTES
    info = read_gguf_model_info(model_path)
    total_layers = info.get("block_count")
    kv_per_token = kv_cache_bytes(info, 1)

    def plan(layers: int, reason: str, ctx: int) -> OffloadPlan:
        return OffloadPlan(
            layers,
            reason,
            n_ctx=ctx,
            requested_n_ctx=n_ctx,
            model_bytes=model_bytes,
            kv_bytes=(kv_per_token * ctx) if kv_per_token else None,
            total_layers=total_layers,
            free_vram_bytes=free,
        )

    if kv_per_token is None:
        # No KV arithmetic available: fall back to weights only, and say so.
        if budget >= model_bytes:
            return plan(
                -1,
                "the weights fit in free VRAM; the KV cache could not be sized "
                "from this model's header",
                n_ctx,
            )
        if not total_layers:
            return plan(
                0,
                "the model does not fit in free VRAM and its layer count is "
                "unreadable, so it runs on the CPU",
                n_ctx,
            )
        fits = max(0, min(total_layers, int(budget / (model_bytes / total_layers))))
        if fits == 0:
            return plan(0, "not enough free VRAM for even one layer, so it runs on the CPU", n_ctx)
        return plan(fits, f"{fits} of {total_layers} layers fit in free VRAM", n_ctx)

    # Everything on the card at the requested context.
    if budget >= model_bytes + kv_per_token * n_ctx:
        return plan(-1, "the model and its KV cache fit in free VRAM", n_ctx)

    # Shrink the context until weights plus KV fit.
    room = budget - model_bytes
    if room > 0:
        fitted_ctx = _round_down_ctx(int(room / kv_per_token))
        if fitted_ctx >= _MIN_GPU_CTX:
            return plan(
                -1,
                f"context reduced from {n_ctx} to {fitted_ctx} so the KV cache fits in VRAM",
                fitted_ctx,
            )

    # Context is already at its floor; give the GPU as many layers as it can hold.
    if total_layers:
        per_layer = model_bytes / total_layers + kv_per_token * _MIN_GPU_CTX / total_layers
        fits = max(0, min(total_layers, int(budget / per_layer))) if budget > 0 else 0
        if fits > 0:
            return plan(
                fits,
                f"{fits} of {total_layers} layers fit at a {_MIN_GPU_CTX}-token context",
                _MIN_GPU_CTX,
            )

    return plan(0, "not enough free VRAM for a useful offload, so it runs on the CPU", n_ctx)
