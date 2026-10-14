"""What a model costs this machine, measured while it loads and answers its checks.

Whether a model fits is decided from a measured figure: how far the free memory of
this machine falls between just before the weights are read and its lowest point
while the model loads and the answer check runs. A small thread samples memory
while that happens; the result is stored with the
answer-check proof (same key, so it belongs to the same weights, backend, device,
driver and library) and read back by model_setup.py.

What is recorded, and what is left null rather than guessed:

  host_fall_bytes     psutil's "available" at the start, less its lowest reading.
                      Never negative; 0 when it never fell. Null when it could not be read.
  rss_*               the engine process's resident memory before and at its peak.
  gpu_bytes           memory this engine holds on the GPU, only where it can be read:
                        Windows  the engine's own share of the adapter's dedicated
                                 memory (the per-process PDH counter), peak less start
                        elsewhere the drop in the offload device's free figure, as ggml
                                 reports it, from before the load to after the checks
                      Null for a CPU load, a backend that reports no figure, or a failed read.
  gpu_shares_host     true when that GPU memory is the machine's own memory (Apple Silicon's
                      unified memory, an integrated GPU), so it is a cost to the host as well.

Free memory alone is noisy on a machine that is in use: the same load read a fall of
0.0, 0.6 and 0.9 GiB in three runs on a Mac with other programs open, while the engine's
resident growth and the Metal allocation were the same every time. host_need_bytes
therefore takes the largest of the readings that are the model's own cost.

The sampler never raises into a load, always ends (stop(), the context manager,
or its own time limit), and a window can be extended over the checks that follow
the load by passing the load's result as `previous`.

The window covers the load and the required answer checks, which include the first
answer (that is when weights the OS has not yet paged in are read). The recorded
checks run later, in the background, so each is measured on its own (Increment) and
widen() adds what it saw to the stored figure. On an Apple M2 Pro, in three runs of
small Qwen models, the largest reading was the long-prompt check at 47 to 87 MiB of
free memory and under 1 MiB on the GPU, a few percent of the figure.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

GIB = 1024 ** 3
INTERVAL_SECONDS = 0.1
# The Windows counters cost more than a memory reading, so they are read every fifth tick.
GPU_EVERY = 5
# A sampler nobody stopped ends itself after this long.
MAX_WINDOW_SECONDS = 1800.0


def read_available() -> int | None:
    """Memory the OS can hand out without swapping, in bytes. None when unreadable."""
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:  # noqa: BLE001 - a measurement must never stop a load
        return None


def read_rss() -> int | None:
    """Resident memory of this process, in bytes. None when unreadable."""
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# GPU memory, where it can be read honestly
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def _apple_silicon() -> bool:
    import platform

    return sys.platform == "darwin" and platform.machine() == "arm64"


class GgmlDeviceFree:
    """The drop in the offload device's free figure since this was created.

    Read only from the thread that loads (never from the sampler thread), because
    it calls into ggml. The figure is device-wide: another program using the same
    card during the window shows up in it too, which is why it is called a drop in
    free memory and not this engine's share.
    """

    source = "ggml_device_free"
    in_thread = False

    def __init__(self, device: dict) -> None:
        if not device.get("total_bytes") or not device.get("free_bytes"):
            raise ValueError("the backend reports no free-memory figure for this device")
        self.shares_host_memory = _apple_silicon() or device.get("type") == "igpu"
        self._name, self._description = device.get("name"), device.get("description")
        self._baseline = int(device["free_bytes"])

    def used(self) -> int | None:
        from potluck_local.services import llama_backend

        for device in llama_backend.describe_devices():
            if device.get("name") == self._name and device.get("description") == self._description:
                if not device.get("total_bytes"):
                    return None
                return max(0, self._baseline - int(device.get("free_bytes") or 0))
        return None


class WindowsOwnShare:
    """This engine's own dedicated GPU memory on the offload adapter, from PDH.

    Matching the adapter reuses llama_backend.system_gpu_use, which is what the
    offload plan already trusts. The per-process counter can be missing (it only
    appears once the process holds GPU memory), so a reading with no entry for
    this adapter is "not read", never zero.
    """

    source = "windows_pdh"
    in_thread = True
    shares_host_memory = False  # an integrated GPU is not matched here (system_gpu_use says "unavailable")

    def __init__(self, device: dict) -> None:
        from potluck_local.services import llama_backend

        use = llama_backend.system_gpu_use(device)
        if use.status != "ok" or not use.luid:
            raise ValueError(use.detail or "the adapter could not be matched")
        self._luid, self._baseline = use.luid, int(use.own_bytes or 0)

    def used(self) -> int | None:
        from potluck_local.services import windows_gpu

        _everyone, own = windows_gpu.pdh_dedicated_usage()
        if self._luid not in own:
            return None
        return max(0, int(own[self._luid]) - self._baseline)


def gpu_meter(device: dict | None):
    """A meter for the device the model is offloaded to, or None when no honest figure exists."""
    if not device:
        return None
    if _is_windows():
        try:
            return WindowsOwnShare(device)
        except Exception as exc:  # noqa: BLE001 - fall back to ggml's own figure
            logger.debug("memory_probe: no per-process GPU counter (%s)", exc)
    try:
        return GgmlDeviceFree(device)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_probe: no GPU figure (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


class MemorySampler:
    """Watches free memory, the engine's resident memory and (optionally) GPU use.

    start() takes the first reading before it returns, so "just before" really is
    just before. stop() joins the thread, takes a last reading and returns the
    figures; it can be called twice. `previous` (the result of an earlier window)
    continues that measurement instead of starting a new baseline, which is how
    the checks extend the load's window.

    threaded=False takes no thread at all: the caller calls sample(). Tests use it.
    """

    def __init__(
        self,
        *,
        previous: dict | None = None,
        gpu: Any = None,
        interval: float | None = None,
        max_seconds: float | None = None,
        available: Callable[[], int | None] | None = None,
        rss: Callable[[], int | None] | None = None,
        threaded: bool = True,
    ) -> None:
        carried = previous or {}
        self._continues = previous is not None
        self._before = carried.get("available_before_bytes")
        self._low = carried.get("available_min_bytes")
        self._rss_before = carried.get("rss_before_bytes")
        self._rss_peak = carried.get("rss_peak_bytes")
        self._gpu_peak = carried.get("gpu_bytes")
        self._samples = int(carried.get("samples") or 0)
        self._carried_seconds = float(carried.get("seconds") or 0.0)
        self._gpu = gpu
        # Read at call time, so a test can shorten them.
        self._interval = INTERVAL_SECONDS if interval is None else interval
        self._max_seconds = MAX_WINDOW_SECONDS if max_seconds is None else max_seconds
        self._threaded = threaded
        self._available, self._rss = available, rss
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._ended: float | None = None

    # -- readings

    @staticmethod
    def _call(reader: Callable[[], int | None] | None, default: Callable[[], int | None]) -> int | None:
        try:
            value = (reader or default)()
        except Exception:  # noqa: BLE001
            return None
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def sample(self, *, gpu: bool = False) -> None:
        available = self._call(self._available, read_available)
        rss = self._call(self._rss, read_rss)
        with self._lock:
            first = self._samples == 0 and not self._continues
            if available is not None:
                if first and self._before is None:
                    self._before = available
                self._low = available if self._low is None else min(self._low, available)
            if rss is not None:
                if first and self._rss_before is None:
                    self._rss_before = rss
                self._rss_peak = rss if self._rss_peak is None else max(self._rss_peak, rss)
            self._samples += 1
        if gpu:
            self._sample_gpu()

    def _sample_gpu(self) -> None:
        if self._gpu is None:
            return
        used = self._call(None, self._gpu.used)
        if used is not None:
            with self._lock:
                self._gpu_peak = max(self._gpu_peak or 0, used)

    # -- lifecycle

    def start(self) -> "MemorySampler":
        self._started = time.monotonic()
        self.sample()
        if self._threaded:
            self._thread = threading.Thread(target=self._run, name="memory-sampler", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        deadline = self._started + self._max_seconds
        ticks = 0
        while not self._halt.wait(self._interval):
            if time.monotonic() > deadline:
                break
            ticks += 1
            self.sample(gpu=bool(self._gpu is not None and self._gpu.in_thread and ticks % GPU_EVERY == 0))

    def stop(self) -> dict:
        if self._ended is None:
            self._halt.set()
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5.0)
            self.sample(gpu=True)
            self._ended = time.monotonic()
        return self.result()

    def __enter__(self) -> "MemorySampler":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def result(self) -> dict:
        with self._lock:
            before, low = self._before, self._low
            fall = None if before is None or low is None else max(0, before - low)
            ended = self._ended if self._ended is not None else time.monotonic()
            gpu = self._gpu_peak
            return {
                "host_fall_bytes": fall,
                "available_before_bytes": before,
                "available_min_bytes": low,
                "rss_before_bytes": self._rss_before,
                "rss_peak_bytes": self._rss_peak,
                "gpu_bytes": gpu,
                "gpu_source": getattr(self._gpu, "source", None) if gpu is not None else None,
                "gpu_shares_host": getattr(self._gpu, "shares_host_memory", None) if gpu is not None else None,
                "samples": self._samples,
                "seconds": round(self._carried_seconds + max(0.0, ended - self._started), 2),
            }


# ---------------------------------------------------------------------------
# Work done after the window closed
# ---------------------------------------------------------------------------


class Increment:
    """What one more piece of work (a recorded answer check, run in the background after the
    load) added to a measurement that was already taken.

    The load's window cannot simply be reopened for it: that window began minutes
    earlier, and anything another program did to free memory or to the GPU in the
    meantime would be counted as the model's. This one has its own baseline, taken as
    the work starts, and reports only the movement during it: the fall in free memory,
    the engine's resident peak (which is absolute), and the GPU growth against the
    reading at the start. widen() adds it to the stored measurement. Start and stop it
    from the thread that runs the work, the same thread that reads ggml's device figure.
    """

    def __init__(self, gpu: Any = None, **sampler: Any) -> None:
        """`sampler`: the MemorySampler options (a test passes its own readers and threaded=False)."""
        self._gpu = gpu
        self._sampler = MemorySampler(gpu=gpu, **sampler)
        self._gpu_before: int | None = None
        self._result: dict | None = None

    def start(self) -> "Increment":
        if self._gpu is not None:
            self._gpu_before = MemorySampler._call(None, self._gpu.used)
        self._sampler.start()
        return self

    def stop(self) -> dict:
        if self._result is None:
            window = self._sampler.stop()
            after, before = window.get("gpu_bytes"), self._gpu_before
            self._result = {
                "host_fall_bytes": window["host_fall_bytes"],
                "rss_peak_bytes": window["rss_peak_bytes"],
                "gpu_added_bytes": max(0, after - before) if after is not None and before is not None else None,
                "samples": window["samples"],
                "seconds": window["seconds"],
            }
        return self._result


def widen(measurement: dict | None, increments: list[dict]) -> dict | None:
    """A stored measurement with work done after it added in, never smaller than it was.

    Free memory and the GPU figure grow by the largest increment, on top of the lowest
    point the load reached. That can read a little high when the load's peak was a
    passing one, and it is the safe side for a figure that says whether a model fits.
    The resident peak is the larger of the two. A figure that was not measured stays
    null. The window is then labelled as covering the checks.

    Widening the same measurement again adds only what is new. The amount already
    added is kept in ``checks_added``, and a later call grows the figure only by how
    far its largest increment exceeds that. Without this, a proof whose recorded
    checks are run again on a later load (they were cut short, or the engine stopped
    first) would gain the same few dozen MiB every time and drift upwards.
    """
    if not isinstance(measurement, dict) or not increments:
        return measurement
    out = dict(measurement)

    def largest(name: str) -> int:
        return max((v for i in increments if isinstance(v := i.get(name), int) and not isinstance(v, bool)), default=0)

    def already(name: str) -> int:
        added = out.get("checks_added")
        value = added.get(name) if isinstance(added, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    fall, gpu = max(largest("host_fall_bytes"), already("host_fall_bytes")), max(largest("gpu_added_bytes"), already("gpu_bytes"))
    more_fall, more_gpu = fall - already("host_fall_bytes"), gpu - already("gpu_bytes")
    if isinstance(out.get("host_fall_bytes"), int):
        out["host_fall_bytes"] += more_fall
        if isinstance(out.get("available_min_bytes"), int):
            out["available_min_bytes"] = max(0, out["available_min_bytes"] - more_fall)
    if isinstance(out.get("gpu_bytes"), int):
        out["gpu_bytes"] += more_gpu
    out["checks_added"] = {"host_fall_bytes": fall, "gpu_bytes": gpu}
    peak = largest("rss_peak_bytes")
    if peak and (not isinstance(out.get("rss_peak_bytes"), int) or peak > out["rss_peak_bytes"]):
        out["rss_peak_bytes"] = peak
    out["samples"] = int(out.get("samples") or 0) + sum(int(i.get("samples") or 0) for i in increments)
    out["seconds"] = round(float(out.get("seconds") or 0.0) + sum(float(i.get("seconds") or 0.0) for i in increments), 2)
    out["window"] = "load+checks"
    return out


# ---------------------------------------------------------------------------
# Reading a stored measurement
# ---------------------------------------------------------------------------


def host_need_bytes(measurement: dict | None) -> int | None:
    """What the host gave up for this model, in bytes, or None when it was not measured.

    The largest of the readings that are the model's own cost, because each can read low:
    the fall in free memory (other programs and the OS move it in both directions), the
    engine's resident growth (it leaves out GPU buffers, and on some systems memory-mapped
    weights), and the GPU allocation where the GPU's memory is the machine's own. None of them
    can read high by more than the load really took, except the fall when another program
    started during the window, which is what the margin in model_setup.py is for.
    """
    if not isinstance(measurement, dict):
        return None
    before, peak = measurement.get("rss_before_bytes"), measurement.get("rss_peak_bytes")
    growth = max(0, peak - before) if isinstance(before, int) and isinstance(peak, int) else None
    gpu = measurement.get("gpu_bytes") if measurement.get("gpu_shares_host") is True else None
    figures = [v for v in (measurement.get("host_fall_bytes"), growth, gpu) if isinstance(v, int) and not isinstance(v, bool) and v >= 0]
    return max(figures) if figures else None
