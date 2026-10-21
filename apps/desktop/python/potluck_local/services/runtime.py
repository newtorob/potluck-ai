"""LLM inference runtime: llama-cpp-python and the opt-in supervised native server.

One process, one loaded model. Batched streams share model access; other
inference and model lifecycle operations retain exclusive access.

Big Model Mode (Potluck Phase G, project=potluck-parallel):
  When `load_model(rpc_servers=...)` is called, the model loads with one or
  more remote ggml-backend RPC servers participating as compute devices.
  The model's weights, KV cache, and compute buffers get distributed across
  the local device + RPC servers according to each device's free memory.
  This is the path for running models too big for any single household
  machine. Requires the Potluck-patched llama-cpp-python wheel.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from potluck_local.services import (
    acceptance,
    inference_errors,
    memory_probe,
    model_evidence,
    model_traits,
    prompt_cache,
    prompt_guard,
    tool_calls,
    tool_markers,
)
from potluck_local.services.model_identity import ModelIdentityError, artifact_identity, file_stamp

logger = logging.getLogger(__name__)

PERSISTED_BENCHMARKS_TTL = 30.0


def _native_model(model) -> bool:
    return bool(getattr(model, "managed_native", False) or getattr(model, "managed_split", False))


# llama.cpp reports what it actually did while loading a model. These are the
# lines that carry it. Parsing the engine's own output is the only way to say
# where the weights ended up without guessing from installed hardware.
_OFFLOAD_RE = re.compile(r"offloaded (\d+)/(\d+) layers to GPU")
_BUFFER_RE = re.compile(r"load_tensors:\s+(\S+) model buffer size =\s+([\d.]+) MiB")
# Weights are only part of what a load puts on the card. A long context can
# make the KV cache larger than the model, which is how a model that "fits"
# still ends up spilling.
_KV_RE = re.compile(r"(\S+) KV buffer size =\s+([\d.]+) MiB")
_COMPUTE_RE = re.compile(r"(\S+) compute buffer size =\s+([\d.]+) MiB")
# "MTL0_Mapped", "CPU_Mapped": a buffer named for its device plus how it is held.
_BUFFER_SUFFIX_RE = re.compile(r"_[A-Za-z]+$")

# Keep placement evidence separately from verbose tensor/scheduler messages.
# Native 0.3.36 emits thousands of those after allocating the weights, which
# would evict the evidence from a ring containing every log line.
_LOG_LINES: deque[str] = deque(maxlen=400)
_LOG_CALLBACK = None  # keeps the ctypes trampoline alive for the process


def _install_log_capture() -> None:
    """Tee llama.cpp's log into a ring buffer, keeping its stderr behaviour.

    llama-cpp-python installs its own global callback at import time which
    prints to stderr when the "llama-cpp-python" logger is verbose enough.
    Replacing it outright would silence the library, so this reproduces that
    gating and captures the text on the way past. Idempotent.
    """
    global _LOG_CALLBACK
    if _LOG_CALLBACK is not None:
        return
    import ctypes
    import sys as _sys

    import llama_cpp

    try:
        from llama_cpp._logger import GGML_LOG_LEVEL_TO_LOGGING_LEVEL as _LEVELS
        from llama_cpp._logger import logger as _lib_logger
    except Exception:  # noqa: BLE001 - private module; capture still works
        _LEVELS, _lib_logger = {}, None

    @llama_cpp.llama_log_callback
    def _callback(level, text, user_data):  # noqa: ANN001
        try:
            decoded = text.decode("utf-8", "replace") if text else ""
        except Exception:  # noqa: BLE001
            return
        if any(pattern.search(decoded) for pattern in (_OFFLOAD_RE, _BUFFER_RE, _KV_RE, _COMPUTE_RE)):
            _LOG_LINES.append(decoded)
        if _lib_logger is not None:
            mapped = _LEVELS.get(level)
            if mapped is not None and _lib_logger.level <= mapped:
                print(decoded, end="", flush=True, file=_sys.stderr)

    llama_cpp.llama_log_set(_callback, ctypes.c_void_p(0))
    _LOG_CALLBACK = _callback


def _parse_load_log(lines: list[str]) -> dict:
    """Pull layer placement and per-device buffer sizes out of llama.cpp's log."""
    offloaded: int | None = None
    total_layers: int | None = None
    buffers: dict[str, float] = {}
    kv: dict[str, float] = {}
    compute: dict[str, float] = {}
    for line in lines:
        m = _OFFLOAD_RE.search(line)
        if m:
            offloaded, total_layers = int(m.group(1)), int(m.group(2))
        m = _BUFFER_RE.search(line)
        if m:
            buffers[m.group(1)] = float(m.group(2))
            continue
        m = _KV_RE.search(line)
        if m:
            kv[m.group(1)] = float(m.group(2))
            continue
        m = _COMPUTE_RE.search(line)
        if m:
            compute[m.group(1)] = float(m.group(2))
    return {
        "offloaded_layers": offloaded,
        "total_layers": total_layers,
        "model_buffer_mib": buffers or None,
        "kv_buffer_mib": kv or None,
        "compute_buffer_mib": compute or None,
    }


# A recorded check that a request cut short is asked again once, after the request has gone.
RECORDED_TRIES = 2


def _mark_reply(reader, result: object) -> None:
    """Tag the marker tokens in a whole reply's text (tool_markers). Never raises."""
    try:
        message = result["choices"][0]["message"]  # type: ignore[index]
        if isinstance(message.get("content"), str):
            message["content"] = reader.whole(message["content"])
    except Exception:  # noqa: BLE001 - never turn a reply into an error
        pass


class _Gate(asyncio.Lock):
    """An asyncio.Lock that counts the callers waiting for it and tells the runtime about it.

    The recorded answer checks run in the background and give way to real work
    (see LLMRuntime._run_recorded). To do that they have to know when a request
    is queued behind them, which asyncio.Lock does not say. `on_wait` is called,
    on the event loop, when a caller is about to wait for a lock that is held;
    `on_change` when a lock was released or a waiter left.
    """

    def __init__(self, on_wait: Callable[[], None] | None = None, on_change: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.waiting = 0
        self._on_wait, self._on_change = on_wait, on_change

    async def acquire(self) -> bool:
        if self._on_wait is not None and self.locked():
            self._on_wait()
        self.waiting += 1
        try:
            return await super().acquire()
        except BaseException:
            self._changed()  # a waiter that gave up may leave the lock free with nobody to wake the checks
            raise
        finally:
            self.waiting -= 1

    def release(self) -> None:
        super().release()
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()


@dataclass(eq=False)
class _RecordedJob:
    """The recorded answer checks of one load, from the moment the verdict was published until they end.

    `interrupt` is how a request stops the check that is generating; `idle` is set
    whenever no check of this job is running in a worker thread, so a synchronous
    unload can wait for the one that is. `stopped` is why the job ended early.
    """

    model: object
    model_id: str | None
    key: dict | None
    level: str
    plan: acceptance.RecordedPlan
    base_checks: list[dict]
    from_proof: bool
    interrupt: threading.Event = field(default_factory=threading.Event)
    idle: threading.Event = field(default_factory=threading.Event)
    finished: list[str] = field(default_factory=list)
    outcomes: list[acceptance.Outcome] = field(default_factory=list)
    increments: list[dict] = field(default_factory=list)
    attempts: int = 0
    stopped: str | None = None
    started: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.idle.set()

    def stop(self, reason: str) -> None:
        if self.stopped is None:
            self.stopped = reason
        self.interrupt.set()


class LLMRuntime:
    """Manages one loaded llama.cpp or transformers inference backend."""

    def __init__(self) -> None:
        self._model = None
        self._split_worker_reserved = False
        self._model_path: str | None = None
        self._artifact: tuple[str, tuple] | None = None
        self._verified_artifact: tuple[str, tuple] | None = None
        self._model_id: str | None = None
        self._loaded_n_ctx: int | None = None
        self._loaded_rpc_servers: tuple[str, ...] | None = None  # Big Model Mode
        # Pipeline-parallel engine (transformers head-loop over the mesh) when a model
        # is loaded with the "pipeline" strategy. Mutually exclusive with self._model.
        self._pipeline = None
        # Optional ContinuousBatcher (whole-model opt-in only; None by default so the
        # single-stream path is completely unchanged when continuous_batch=False).
        self._batcher = None
        self._load_lock = threading.Lock()
        self._inference_lock = _Gate(on_wait=self._request_is_waiting, on_change=self._wake_recorded)
        # A batch of streams collectively owns _inference_lock. The admission
        # gate lets lifecycle operations stop new admissions before draining
        # the current batch, so an ongoing stream of requests cannot starve a swap.
        self._admission_lock = _Gate(on_change=self._wake_recorded)
        self._batched_streams = 0
        # The recorded answer checks of the loaded model, running in the background after its
        # verdict (see _run_recorded). `_background_holds` is true while one of them holds
        # _inference_lock, which is not a request and does not make this machine busy.
        self._recorded_job: _RecordedJob | None = None
        self._recorded_task: asyncio.Task | None = None
        self._recorded_loop: asyncio.AbstractEventLoop | None = None
        self._recorded_watchers: list[asyncio.Future] = []
        self._background_holds = False
        # Last benchmark result per model_id (tokens/sec + TTFT). Persists across
        # loads so the Models page + peer rows can show "last bench" without
        # re-running. Populated by benchmark().
        self._benchmarks: dict[str, dict] = {}
        # Results kept from before this process started (services/model_evidence.py),
        # read at most every PERSISTED_BENCHMARKS_TTL seconds: peers ask often.
        self._persisted_benchmarks: tuple[float, dict[str, dict]] | None = None
        # What llama.cpp reported while loading the current model: which
        # backend was selected, how many layers it actually put on the GPU,
        # and the per-device weight buffers. None when nothing is loaded.
        self._load_report: dict | None = None
        # The answer check of the loaded model (services/acceptance.py), in the
        # shape /health/inference reports, and what is needed to load it again
        # on the CPU. None when nothing was checked.
        self._acceptance: dict | None = None
        self._acceptance_tokens: int | None = None
        self._load_args: dict | None = None
        # What loading the current model cost this machine (services/memory_probe.py):
        # the window's figures so far, and the GPU meter that goes with them. The checks
        # extend the window; the proof stores it. None when nothing was measured.
        self._load_memory: dict | None = None
        self._gpu_meter = None
        # Ensure unload runs before ggml-metal's atexit finalize on process exit.
        self._atexit_registered = False

    def is_loaded(self) -> bool:
        if _native_model(self._model) and self._model.closed:
            return False
        return self._model is not None or self._pipeline is not None

    def proves_tool_markers(self) -> frozenset[str]:
        """The tool-call markers whose token this machine's model can prove were sampled
        (see tool_markers.py). Empty for a pipeline, a split model or an unloaded engine."""
        model = self._model
        if self._pipeline is not None or self._batcher is not None or model is None:
            return frozenset()
        try:
            return tool_markers.proven(model)
        except Exception:  # noqa: BLE001
            return frozenset()

    def current_model_id(self) -> str | None:
        return self._model_id

    def _gpu_offload_active(self) -> bool:
        """Whether the loaded model has layers on a GPU. Used only to decide if a
        native C++ exception can be a device allocation failure (a CPU load cannot
        throw one)."""
        report = self._load_report or {}
        offloaded = report.get("offloaded_layers")
        if offloaded is not None:
            return offloaded > 0
        return bool(report.get("used_n_gpu_layers"))

    def current_model_sha256(self) -> str | None:
        if _native_model(self._model) and self._model.closed:
            return None
        if not self._artifact or not self._model_path or self._pipeline is not None or self._loaded_rpc_servers:
            return None
        try:
            path = Path(self._model_path)
            if file_stamp(path) == self._artifact[1]:
                if self._verified_artifact != self._artifact:
                    # A freshly written file can change within one filesystem
                    # timestamp tick. Recheck bytes until that tick is safely
                    # past, including the first observation after native load.
                    # Established weights then use the cheap metadata check.
                    checked = artifact_identity(path)
                    if checked != self._artifact:
                        self._artifact = None
                        return None
                    if time.time_ns() - max(checked[1][-2:]) > 2_000_000_000:
                        self._verified_artifact = checked
                return self._artifact[0]
        except OSError:
            pass
        return None

    def assert_model_identity(self, model: str | None, sha256: str | None = None) -> None:
        # Call again after inference admission: route selection can race a load.
        if model is not None and (not self.is_loaded() or model != self._model_id):
            raise ModelIdentityError("Requested model is no longer loaded. Load the requested model and retry.")
        if sha256 is not None and self.current_model_sha256() != sha256:
            raise ModelIdentityError("Loaded model does not match the project SHA-256, or this backend cannot enforce a GGUF lock. Load the exact locked GGUF locally and retry.")

    def loaded_n_ctx(self) -> int | None:
        """Return the n_ctx the currently loaded model was initialised with, or None."""
        return self._loaded_n_ctx

    def template_traits(self) -> "model_traits.TemplateTraits | None":
        """What the loaded llama.cpp model's chat template can do (tools, an opened
        reasoning block). None when nothing is loaded or the backend cannot say."""
        if self._pipeline is not None:
            return None
        native_traits = getattr(self._model, "template_traits", None)
        if isinstance(native_traits, model_traits.TemplateTraits):
            return native_traits if self.is_loaded() else None
        return model_traits.traits_of_model(self._model)

    async def count_tokens(self, text: str) -> int | None:
        """Tokens in ``text`` by the loaded llama.cpp model's own tokenizer, or None
        when this runtime cannot say (nothing loaded, or a backend without one).
        Never loads a model. Waits for a load or unload in progress, so the native
        model cannot be freed while it is read."""
        async with self._admission_lock:
            tokenize = getattr(self._model, "tokenize", None)
            if not callable(tokenize):
                return None
            try:
                if _native_model(self._model):
                    # Native tokenization is loopback HTTP, not a cheap in-process call.
                    # Drain it before a lifecycle operation can free the child.
                    worker = asyncio.get_running_loop().run_in_executor(
                        None, partial(tokenize, text.encode("utf-8", "replace"), add_bos=False),
                    )
                    try:
                        return len(await asyncio.shield(worker))
                    finally:
                        await self._drain_worker(worker)
                return len(tokenize(text.encode("utf-8", "replace"), add_bos=False))
            except Exception as exc:  # noqa: BLE001 - a count is a convenience, never a failure
                logger.debug("count_tokens_failed error=%s", type(exc).__name__)
                return None

    def busy(self) -> bool:
        """Whether a request is using the model or waiting for it. A recorded answer check
        running in the background does not count: it gives way to the request."""
        gate = self._inference_lock
        return gate.waiting > 0 or (gate.locked() and not self._background_holds)

    def loaded_rpc_servers(self) -> tuple[str, ...] | None:
        """Return the RPC server endpoints the current model was loaded with.
        None when the model is loaded locally (or no model is loaded)."""
        return self._loaded_rpc_servers

    def loaded_pipeline(self) -> dict | None:
        """Describe the loaded pipeline-parallel engine (model, head layers, downstream
        peer, device), or None when not in pipeline mode."""
        return self._pipeline.describe() if self._pipeline is not None else None

    def continuous_batch_active(self) -> bool:
        """True when the loaded model is being served through a continuous batcher
        (ContinuousBatcher for whole-model, DistributedContinuousBatcher for pipeline).
        False for the single-stream path and when no model is loaded. /health reads this
        so the frontend can show the shared-batching toggle as on/off."""
        return self._batcher is not None

    def benchmarks(self) -> dict[str, dict]:
        """Last benchmark result keyed by model_id (tokens/sec + TTFT).

        Results from before a restart are included while they still describe this
        file, backend, driver and library; one measured in this process wins."""
        merged = self._remembered_benchmarks()
        merged.update(self._benchmarks)
        return merged

    def _remembered_benchmarks(self) -> dict[str, dict]:
        cached = self._persisted_benchmarks
        if cached is not None and time.monotonic() - cached[0] < PERSISTED_BENCHMARKS_TTL:
            return dict(cached[1])
        from potluck_local.utils.paths import models_dir

        loaded = None
        if self._model is not None and self._model_id:
            report = self._load_report or {}
            loaded = {"id": self._model_id, "cpu_only": bool(report.get("cpu_only")),
                      "n_gpu_layers": report.get("used_n_gpu_layers", 0)}
        found = model_evidence.persisted_benchmarks(models_dir(), loaded)
        self._persisted_benchmarks = (time.monotonic(), found)
        return dict(found)

    def inference_summary(self) -> dict | None:
        """Where the loaded model is actually executing, as llama.cpp reported it.

        None when no model is loaded through the llama.cpp path. The shape is
        the additive `inference` block described in the companion readiness
        contract, and `execution_verified` is true only when the engine's own
        load output accounted for this exact model's weights. A detected
        graphics card, a backend that supports offload, or a requested layer
        count are not evidence and never set it.
        """
        report = self._load_report
        if not report or self._model is None:
            return None

        buffers: dict[str, float] = report.get("model_buffer_mib") or {}
        offloaded = report.get("offloaded_layers")
        total = report.get("total_layers")

        # Evidence is the per-device weight buffers llama.cpp printed for this
        # load. Without them we know nothing and say so.
        verified = bool(buffers)

        # "CUDA_Host" / "Vulkan_Host" are pinned system memory, not device memory.
        # A device's weights can also be named with a suffix ("MTL0_Mapped" on
        # Metal); the device is the name without it.
        gpu_buffers: dict[str, float] = {}
        for name, size in buffers.items():
            if not name.upper().startswith("CPU") and "HOST" not in name.upper():
                device = _BUFFER_SUFFIX_RE.sub("", name)
                gpu_buffers[device] = gpu_buffers.get(device, 0.0) + size
        if not verified:
            device_kind = "unknown"
        elif offloaded is not None and total:
            device_kind = "gpu" if offloaded >= total else ("cpu" if offloaded == 0 else "mixed")
        else:
            device_kind = "gpu" if gpu_buffers else "cpu"

        # The buffer name is the ggml device name ("Vulkan0", "CUDA0", "Metal",
        # "CPU_Mapped"), so the backend label comes from the engine too.
        backend = "cpu"
        device_name = "unknown"
        selection = report.get("backend_selection") or {}
        devices = {d.get("name"): d for d in (selection.get("devices") or [])}
        if gpu_buffers:
            primary = max(gpu_buffers, key=lambda k: gpu_buffers[k])
            backend = re.sub(r"\d+$", "", primary).lower() or "gpu"
            device_name = (devices.get(primary) or {}).get("description") or primary
        else:
            cpu_dev = devices.get("CPU") or {}
            device_name = cpu_dev.get("description") or "CPU"
        if not verified and report.get("engine") == "llama-server":
            backend = device_name = "unknown"

        return {
            "schema_version": 1,
            "model_id": self._model_id,
            "execution_verified": verified,
            "device_kind": device_kind,
            "device_name": device_name,
            "backend": backend,
        }

    def inference_details(self) -> dict | None:
        """The full load report behind `inference_summary`, for the diagnostics UI."""
        if not self._load_report or self._model is None:
            return None
        details = dict(self._load_report)
        details["summary"] = self.inference_summary()
        return details

    def load_model(
        self,
        model_path: str,
        *,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,
        n_threads: int | None = None,
        rpc_servers: list[str] | None = None,
        cpu_only: bool = False,
    ) -> dict:
        """Load a GGUF model. Blocks until loaded. Thread-safe swap via _load_lock.

        cpu_only: no layers on a GPU and no GPU help with prompt batches either.
        The answer check loads this way to tell a broken GPU from a weak model.

        rpc_servers: optional list of llama.cpp RPC server endpoints (e.g.
        ["100.64.0.6:50052"]) to register as remote ggml-backend devices.
        When set, llama.cpp distributes weights + KV cache + compute across
        the local device(s) + RPC servers in proportion to each device's
        free memory. This is "Big Model Mode" — used when a model is too
        large to fit on any single household machine alone.
        """
        from potluck_local.services import llama_backend

        # Decided once per process, before llama_cpp is imported anywhere.
        selection = llama_backend.current_selection()

        from llama_cpp import Llama  # lazy import — avoids Metal init at import time

        _install_log_capture()

        with self._load_lock:
            if self._model is not None or self._pipeline is not None:
                self._release_model()

            requested_gpu_layers = n_gpu_layers
            requested_n_ctx = n_ctx
            plan = None
            selected_device = None
            if selection.backend == "cpu" or cpu_only:
                plan = llama_backend.OffloadPlan(
                    0,
                    selection.reason if selection.backend == "cpu" else "the answer check is trying the CPU",
                    n_ctx=n_ctx,
                    requested_n_ctx=n_ctx,
                )
                n_gpu_layers = 0
            elif selection.probed:
                # A backend this module selected after a probe: size the offload
                # against VRAM that is free right now. Everywhere else (macOS
                # Metal, custom builds) keeps the caller's value untouched.
                devices = llama_backend.describe_devices()
                if not rpc_servers:
                    selected_device = llama_backend.offload_device(devices)
                plan = llama_backend.plan_gpu_layers(
                    model_path,
                    requested=n_gpu_layers,
                    n_ctx=n_ctx,
                    devices=devices,
                )
                n_gpu_layers = plan.n_gpu_layers
                if plan.n_ctx and plan.n_ctx != n_ctx:
                    n_ctx = plan.n_ctx
                if plan.system_use is not None:
                    # Windows only: what the rest of the machine is using, or why
                    # that could not be read.
                    use = plan.system_use
                    if use["status"] == "ok":
                        logger.info(
                            "gpu_memory_check status=ok adapter=%s matched_by=%s source=%s "
                            "total_bytes=%s other_programs_bytes=%s driver_free_bytes=%s "
                            "reserve_bytes=%s budget_bytes=%s",
                            use["adapter"],
                            use["matched_by"],
                            use["source"],
                            plan.vram_total_bytes,
                            plan.other_programs_vram_bytes,
                            plan.reported_free_vram_bytes,
                            plan.reserve_bytes,
                            plan.budget_bytes,
                        )
                    else:
                        logger.info(
                            "gpu_memory_check status=unavailable detail=%s driver_free_bytes=%s",
                            use["detail"],
                            plan.reported_free_vram_bytes,
                        )
                if (
                    plan.n_gpu_layers != requested_gpu_layers
                    or plan.n_ctx != requested_n_ctx
                    or plan.system_use is not None
                ):
                    logger.info(
                        "gpu_offload_planned requested_layers=%s using_layers=%s "
                        "requested_n_ctx=%s using_n_ctx=%s reason=%s",
                        requested_gpu_layers,
                        plan.n_gpu_layers,
                        requested_n_ctx,
                        n_ctx,
                        plan.reason,
                    )

            start = time.monotonic()
            effective_threads = n_threads or 0
            kwargs: dict = {
                "model_path": model_path,
                "n_ctx": n_ctx,
                "n_gpu_layers": n_gpu_layers,
                "n_threads": effective_threads,
                "verbose": False,
            }
            if selected_device is not None:
                from llama_cpp import LLAMA_SPLIT_MODE_NONE

                # Size and execute on the same device. llama.cpp's default
                # layer split could otherwise place weights on an iGPU based
                # on its much larger shared-memory figure.
                kwargs["split_mode"] = LLAMA_SPLIT_MODE_NONE
                kwargs["main_gpu"] = selected_device[0]
            if cpu_only:
                # With layers on the CPU, a GPU backend still takes large prompt
                # batches unless told not to. Older wrappers have no such switch.
                import inspect

                if "op_offload" in inspect.signature(Llama.__init__).parameters:
                    kwargs["op_offload"] = False
            if rpc_servers:
                # Only thread the param through when set — keeps the call
                # backwards-compatible with stock (non-Potluck-patched)
                # llama-cpp-python wheels that don't accept the kwarg.
                kwargs["rpc_servers"] = rpc_servers

            # Bind the digest to the file actually loaded, never to a catalog or sidecar claim.
            # An unreadable/changing file remains ineligible for locked inference.
            try:
                identity = artifact_identity(Path(model_path))
            except OSError:
                identity = None
            _LOG_LINES.clear()
            # The memory window opens here: the previous model is already gone and the
            # weights have not been read yet. The answer check extends it (see _run_checks_now).
            meter = None if rpc_servers or n_gpu_layers == 0 else self._gpu_meter_for(selected_device)
            sampler = None if rpc_servers else self._open_memory_window(meter)
            try:
                model = Llama(**kwargs)
            finally:
                window = sampler.stop() if sampler is not None else None
            model_traits.fit_model(model)
            verified_identity = None
            try:
                if identity:
                    # Verify again in the loading worker, keeping a full-file
                    # hash out of the first peer capability/inference request.
                    checked = artifact_identity(Path(model_path))
                    if checked != identity:
                        identity = None
                    elif time.time_ns() - max(checked[1][-2:]) > 2_000_000_000:
                        verified_identity = checked
            except OSError:
                identity = None
            elapsed = round(time.monotonic() - start, 2)
            load_log = _parse_load_log(list(_LOG_LINES))

            self._model = model
            self._model_path = model_path
            self._artifact = identity
            self._verified_artifact = verified_identity
            self._model_id = Path(model_path).parent.name
            self._loaded_n_ctx = n_ctx
            self._loaded_rpc_servers = tuple(rpc_servers) if rpc_servers else None
            self._load_memory = {**window, "window": "load"} if window else None
            self._gpu_meter = meter if window else None
            self._persisted_benchmarks = None
            self._attach_prompt_cache(model, identity, rpc_servers)
            backend_selection = selection.as_dict()
            if not backend_selection["devices"]:
                # Metal, or a wheel with one backend set: nothing was probed, so
                # ask ggml now. This is what names the device in inference_summary.
                backend_selection["devices"] = llama_backend.describe_devices()
            self._load_report = {
                "backend_selection": backend_selection,
                "requested_n_gpu_layers": requested_gpu_layers,
                "used_n_gpu_layers": n_gpu_layers,
                "requested_n_ctx": requested_n_ctx,
                "used_n_ctx": n_ctx,
                "offload_plan": plan.as_dict() if plan else None,
                "offload_device": selected_device[1] if selected_device else None,
                "cpu_only": cpu_only,
                **load_log,
            }
            # Register after Llama() so our atexit runs before ggml-metal's
            # (atexit is LIFO). Prevents the macOS "python quit unexpectedly /
            # Reopen" dialog when Metal finalize races process exit.
            if not self._atexit_registered:
                atexit.register(self.unload_model)
                self._atexit_registered = True

            logger.info(
                "model_loaded model_id=%s n_ctx=%d n_gpu_layers=%d n_threads=%s "
                "rpc_servers=%s load_time=%.2fs backend=%s offloaded=%s/%s",
                self._model_id,
                n_ctx,
                n_gpu_layers,
                effective_threads or "auto",
                ",".join(rpc_servers) if rpc_servers else "(none)",
                elapsed,
                selection.backend,
                load_log.get("offloaded_layers"),
                load_log.get("total_layers"),
            )

            return {
                "model_path": model_path,
                "model_id": self._model_id,
                "load_time_seconds": elapsed,
                "n_ctx": n_ctx,
                "n_gpu_layers": n_gpu_layers,
                "n_threads": effective_threads or None,
                "rpc_servers": list(rpc_servers) if rpc_servers else None,
                "inference": self.inference_summary(),
            }

    @staticmethod
    def _gpu_meter_for(selected_device):
        """The meter for the device layers are offloaded to, or None when no honest GPU figure exists."""
        from potluck_local.services import llama_backend

        try:
            chosen = selected_device or llama_backend.offload_device(llama_backend.describe_devices())
            return memory_probe.gpu_meter(chosen[1]) if chosen else None
        except Exception:  # noqa: BLE001 - a measurement must never stop a load
            return None

    @staticmethod
    def _open_memory_window(meter):
        try:
            return memory_probe.MemorySampler(gpu=meter).start()
        except Exception:  # noqa: BLE001
            logger.debug("memory_window_not_started", exc_info=True)
            return None

    def load_pipeline(
        self,
        model_id: str,
        *,
        split: int,
        peer: str,
        port: int,
        split2: int = 0,
        device: str = "",
        dtype: str | None = None,
        draft_model: str | None = None,
        draft_k: int = 4,
        continuous_batch: bool = False,
    ) -> dict:
        """Load a model in PIPELINE-PARALLEL strategy: this node holds the head layer
        block and drives generation across downstream peer stages (mid/tail) over the
        mesh. Distinct from the llama.cpp path -- it is transformers-based, references an
        HF model id (not a GGUF slug), and runs a model too big for any single peer.
        Scope: my-machines / trusted-circle only (activation privacy unsolved). Blocks
        until the head block is loaded; thread-safe swap via _load_lock.

        draft_model (opt-in): an HF model id for a small DRAFT model loaded locally on the
        head. When set, generation uses SPECULATIVE DECODING -- the draft proposes draft_k
        tokens per round and the chain verifies all K+1 in one round-trip, amortizing WAN
        latency. Output is identical to greedy. Unset => the default greedy path, unchanged.

        continuous_batch=False (default): single-stream chain path, byte-for-byte unchanged.
        continuous_batch=True: a DistributedContinuousBatcher wraps the chain so concurrent
        requests share one chain round-trip per decode step (the tail/mid stages already
        speak the cb_prefill/cb_step protocol). Mutually exclusive with draft_model -- the
        speculative path is single-stream only; passing both raises. Shut down on unload.
        """
        if continuous_batch and draft_model:
            raise ValueError(
                "continuous_batch and draft_model are mutually exclusive: speculative "
                "decoding is a single-stream path; continuous batching shares the chain "
                "across concurrent requests instead."
            )
        # Lazy import: pulls in torch + transformers + accelerate, kept out of the base
        # sidecar so a non-pipeline install stays light.
        from .pipeline_parallel import PipelineParallelEngine

        torch_dtype = None
        if dtype:
            import torch
            torch_dtype = getattr(torch, dtype)

        with self._load_lock:
            if self._model is not None or self._pipeline is not None:
                self._release_model()

            start = time.monotonic()
            engine = PipelineParallelEngine(
                model_id, split=split, peer=peer, port=port,
                split2=split2, device=device, dtype=torch_dtype,
                draft_model=draft_model, draft_k=draft_k,
            )
            self._pipeline = engine
            self._model_id = model_id

            # Opt-in distributed continuous batcher: drives the same chain but shares one
            # round-trip per decode step across concurrent requests. Built AFTER the chain
            # is up (it needs the loaded engine's parts/tokenizer/peer/port). Default off so
            # the existing single-stream + speculative chain paths are byte-for-byte
            # unchanged. Stopped in _release_model (closes its persistent tail socket too).
            if continuous_batch:
                from .continuous_batch import DistributedContinuousBatcher
                self._batcher = DistributedContinuousBatcher(engine)
                logger.info(
                    "distributed_continuous_batcher_enabled model_id=%s", model_id,
                )
            else:
                self._batcher = None

            elapsed = round(time.monotonic() - start, 2)

            logger.info(
                "pipeline_loaded model_id=%s split=%s split2=%s downstream=%s:%s "
                "continuous_batch=%s load_time=%.2fs",
                model_id, split, split2, peer, port, continuous_batch, elapsed,
            )
            return {
                "model_id": model_id,
                "strategy": "pipeline",
                "load_time_seconds": elapsed,
                "continuous_batch": continuous_batch,
                **engine.describe(),
            }

    def load_whole_model(
        self,
        model_id: str,
        *,
        device: str = "",
        dtype: str | None = None,
        continuous_batch: bool = False,
    ) -> dict:
        """Load a FULL HF model locally and serve it -- the WHOLE-MODEL path. This is
        the auto-pipeline planner's mode=="whole" fallback: the model fits on this
        machine's own VRAM, so there is no layer split and no peer chain, just plain
        local transformers inference.

        Slot choice: WholeModelEngine presents the IDENTICAL stream_tokens + describe
        surface as PipelineParallelEngine, so it occupies the SAME self._pipeline slot.
        stream_chat / _release_model / loaded_pipeline therefore all work unchanged --
        no whole-model special-casing anywhere else in the runtime. The three backends
        (llama _model / pipeline / whole-model) stay mutually exclusive because they
        share that one slot pair (_model XOR _pipeline). describe()["strategy"] is the
        discriminator ("pipeline" vs "whole-model") on the wire.

        continuous_batch=False (default): single-stream path, byte-for-byte unchanged.
        continuous_batch=True: a ContinuousBatcher is created and stream_chat routes
        concurrent requests through shared batched forward passes instead of the
        inference lock. The batcher is shut down on unload_model().

        Blocks until the model is loaded; thread-safe swap via _load_lock.
        """
        # Lazy import: WholeModelEngine lives in pipeline_parallel, which top-imports
        # torch + transformers. Mirrors load_pipeline -- keep the base sidecar light;
        # only the path that actually loads a model drags torch in.
        from .pipeline_parallel import WholeModelEngine

        torch_dtype = None
        if dtype:
            import torch
            torch_dtype = getattr(torch, dtype)

        with self._load_lock:
            if self._model is not None or self._pipeline is not None:
                self._release_model()

            start = time.monotonic()
            engine = WholeModelEngine(model_id, device=device, dtype=torch_dtype)
            self._pipeline = engine
            self._model_id = model_id
            elapsed = round(time.monotonic() - start, 2)

            # Opt-in continuous batcher: wraps the engine, intercepting stream_chat
            # so concurrent requests share batched forward passes.  Default off so
            # the existing single-stream path is byte-for-byte unchanged.
            if continuous_batch:
                from .continuous_batch import ContinuousBatcher
                self._batcher = ContinuousBatcher(engine)
                logger.info(
                    "continuous_batcher_enabled model_id=%s", model_id,
                )
            else:
                self._batcher = None

            logger.info(
                "whole_model_loaded model_id=%s device=%s dtype=%s load_time=%.2fs continuous_batch=%s",
                model_id, engine.device, engine.dtype, elapsed, continuous_batch,
            )
            return {
                "model_id": model_id,
                "strategy": "whole-model",
                "load_time_seconds": elapsed,
                "continuous_batch": continuous_batch,
                **engine.describe(),
            }

    def _attach_prompt_cache(self, model, identity, rpc_servers) -> None:
        """Give an eligible in-process model a disk prompt cache (services/prompt_cache.py)."""
        self._prompt_cache = None
        try:
            from potluck_local.config import get_local_settings
            from potluck_local.services import prompt_cache
            from potluck_local.services.settings_manager import get_settings

            settings = get_settings()
            runtime_settings = settings.runtime
            # Saved prompts are chat content: they follow the chat history setting,
            # and the cache's own switch can still turn them off.
            if not (settings.privacy.chat_history_enabled and runtime_settings.prompt_cache) \
                    or rpc_servers or not identity:
                return
            reason = prompt_cache.eligibility(getattr(model, "metadata", None))
            if reason:
                logger.info("prompt_cache_off model_id=%s reason=%s", self._model_id, reason)
                return
            store = prompt_cache.PromptCacheStore(
                prompt_cache.cache_root(get_local_settings().data_dir),
                max_bytes=int(runtime_settings.prompt_cache_max_gib * 1024 ** 3),
                ttl_seconds=runtime_settings.prompt_cache_ttl_hours * 3600)
            store.sweep()
            cache = prompt_cache.PromptDiskCache(store, model, identity[0])
            model.set_cache(cache)
            self._prompt_cache = cache
        except Exception:  # noqa: BLE001 - the cache is an optimisation, never a load failure
            logger.warning("prompt_cache_attach_failed", exc_info=True)
            self._prompt_cache = None

    def _detach_prompt_cache(self, model) -> None:
        """Write the pending and live prompt state before the model is freed."""
        cache, self._prompt_cache = getattr(self, "_prompt_cache", None), None
        if cache is not None:
            try:
                cache.close()
            except Exception:  # noqa: BLE001
                logger.warning("prompt_cache_detach_failed", exc_info=True)
            with suppress(Exception):
                model.set_cache(None)

    def unload_model(self) -> None:
        """Release the current model and free memory."""
        with self._load_lock:
            self._release_model()

    # -----------------------------------------------------------------
    # Async lifecycle wrappers — the ONLY load/unload entrypoints the HTTP
    # routes should call.
    #
    # Root cause of the launch-blocking race this closes: _release_model()
    # (called by unload_model, and internally by load_model/load_pipeline/
    # load_whole_model before swapping in a new model) used to run with no
    # coordination against _inference_lock -- the asyncio.Lock that
    # stream_chat/chat/stream_chat_raw/score_sequence/benchmark hold for the
    # full duration of an active generation. A worker thread can be mid
    # `model.create_chat_completion()` while the event-loop thread runs
    # unload_model() -> _release_model() -> model.close(), freeing the
    # llama.cpp handle out from under the still-running worker thread
    # (use-after-free). The old GGUF load path made this worse by calling
    # the blocking `Llama(...)` constructor directly on the event loop
    # (routers/models.py), so a multi-second load also stalled every other
    # in-flight request on this process.
    #
    # These wrappers acquire the SAME _inference_lock generation holds
    # before touching the model, so a lifecycle op can only proceed once any
    # active generation has fully finished (and, symmetrically, a new
    # generation can't start mid-swap) -- and they run the actual blocking
    # work (Llama() construction / model.close()) in a thread-pool executor
    # so holding the lock never blocks the event loop.
    async def load_model_async(
        self,
        model_path: str,
        *,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,
        n_threads: int | None = None,
        rpc_servers: list[str] | None = None,
        verify_answers: bool | None = None,
        backend: str = "llama-cpp-python",
        native_cache_mib: int = 256,
        expected_model_sha256: str | None = None,
        expected_engine_acceleration: str | None = None,
    ) -> dict:
        """verify_answers: True checks the model's answers as part of the load
        (and may move it to the CPU), False records that the owner turned the
        check off, None leaves the check alone."""
        if self._split_worker_reserved:
            raise RuntimeError("This machine is reserved as a split worker. Run potluck split worker off first.")
        if backend == "llama-server":
            if rpc_servers:
                raise ValueError("The native local backend cannot use rpc_servers; use managed split mode.")
            return await self._run_lifecycle(partial(
                self._load_local_server, model_path, n_ctx=n_ctx, n_gpu_layers=n_gpu_layers,
                n_threads=n_threads, cache_mib=native_cache_mib,
                **({"expected_model_sha256": expected_model_sha256,
                    "expected_engine_acceleration": expected_engine_acceleration}
                   if expected_model_sha256 is not None or expected_engine_acceleration is not None
                   else {}),
            ))
        if expected_model_sha256 is not None or expected_engine_acceleration is not None:
            raise ValueError("Pinned engine recipes require the native local backend")
        if backend != "llama-cpp-python":
            raise ValueError("Unknown inference backend")
        load = partial(self.load_model, model_path, n_ctx=n_ctx,
                       n_gpu_layers=n_gpu_layers, n_threads=n_threads,
                       rpc_servers=rpc_servers)
        if verify_answers is not None:
            load = partial(self._load_with_check, model_path, verify=verify_answers, n_ctx=n_ctx,
                           n_gpu_layers=n_gpu_layers, n_threads=n_threads, rpc_servers=rpc_servers)
        try:
            return await self._run_lifecycle(load)
        finally:
            # The load has returned and both gates are free: a request that was waiting for them has
            # the model first. The recorded checks start after that, if the load left any to run.
            self._launch_recorded()

    def _load_local_server(self, model_path, *, n_ctx, n_gpu_layers, n_threads, cache_mib,
                           expected_model_sha256=None, expected_engine_acceleration=None):
        from potluck_local.services import local_server

        if n_ctx < 512 or n_gpu_layers < -1 or (n_threads is not None and n_threads < 0):
            raise ValueError("Invalid native engine resource settings")
        if not 0 <= cache_mib <= 4096:
            raise ValueError("Native prompt cache must be between 0 and 4096 MiB")
        # Validate availability and the exact file before freeing a working model.
        root, manifest = local_server.bundle()
        if expected_engine_acceleration is not None:
            if expected_engine_acceleration not in ("cpu", "metal", "cuda"):
                raise ValueError("Invalid recipe engine acceleration")
            if ((expected_engine_acceleration == "cpu" and n_gpu_layers != 0)
                    or (expected_engine_acceleration != "cpu"
                        and (manifest.get("backend") != expected_engine_acceleration
                             or n_gpu_layers != -1))):
                raise ValueError("The native engine no longer matches this recipe. Run preflight again.")
        identity = artifact_identity(Path(model_path))
        if expected_model_sha256 is not None and identity[0] != expected_model_sha256:
            raise ModelIdentityError("The installed model does not match this recipe's SHA-256.")
        with self._load_lock:
            self._release_model()
            start = time.monotonic()
            model = local_server.start(model_path, root=root, n_ctx=n_ctx,
                                       n_gpu_layers=n_gpu_layers, n_threads=n_threads,
                                       cache_mib=cache_mib)
            try:
                if artifact_identity(Path(model_path)) != identity:
                    raise ModelIdentityError("The model file changed during loading. Reload the unchanged file.")
            except BaseException:
                model.close()
                raise
            self._model = model
            self._model_path = model_path
            self._model_id = Path(model_path).parent.name
            self._artifact = identity
            # Preserve the first-use recheck for a file just written in this timestamp tick.
            self._verified_artifact = None
            self._loaded_n_ctx = n_ctx
            self._loaded_rpc_servers = None
            self._persisted_benchmarks = None
            self._load_report = {
                "engine": "llama-server", "engine_commit": manifest["commit"],
                "requested_n_ctx": n_ctx, "used_n_ctx": n_ctx,
                "requested_n_gpu_layers": n_gpu_layers,
                "native_cache_mib": cache_mib, "flash_attention": "auto",
                "cpu_only": n_gpu_layers == 0,
            }
            if not self._atexit_registered:
                atexit.register(self.unload_model)
                self._atexit_registered = True
            return {"model_id": self._model_id, "model_path": model_path,
                    "load_time_seconds": round(time.monotonic() - start, 2),
                    "n_ctx": n_ctx, "n_threads": n_threads, "backend": "llama-server",
                    "engine_commit": manifest["commit"], "native_cache_mib": cache_mib,
                    "inference": self.inference_summary(), "answers_verified": False}

    async def load_pipeline_async(self, model_id: str, **kwargs) -> dict:
        if self._split_worker_reserved:
            raise RuntimeError("This machine is reserved as a split worker.")
        return await self._run_lifecycle(partial(self.load_pipeline, model_id, **kwargs))

    async def load_whole_model_async(self, model_id: str, **kwargs) -> dict:
        if self._split_worker_reserved:
            raise RuntimeError("This machine is reserved as a split worker.")
        return await self._run_lifecycle(partial(self.load_whole_model, model_id, **kwargs))

    async def unload_model_async(self) -> None:
        await self._run_lifecycle(self.unload_model)

    async def install_split_async(self, model, path, slug, identity, context):
        def install():
            if self._split_worker_reserved:
                raise RuntimeError("This machine is reserved as a split worker.")
            with self._load_lock:
                self._release_model()
                self._model = model
                self._model_path = str(path)
                self._model_id = slug
                self._artifact = self._verified_artifact = identity
                self._loaded_n_ctx = context
        return await self._run_lifecycle(install)

    async def release_split_async(self, model):
        # A normal load may have replaced the adapter while its asynchronous
        # close callback was waiting. Never unload that newer model.
        def release():
            if self._model is model:
                self.unload_model()
        await self._run_lifecycle(release)

    # -----------------------------------------------------------------
    # Answer check. The checks, the proofs and the sentences are in
    # services/acceptance.py; this is where they meet the loaded model.
    # Everything here runs on the lifecycle worker with both gates held, so
    # no request is served between a load and its check, and a move to the
    # CPU cannot race one.

    @staticmethod
    def _checking_answers() -> bool:
        """runtime.verify_answers, read live: turning it off takes effect at once."""
        from potluck_local.services.settings_manager import get_settings

        try:
            return bool(get_settings().runtime.verify_answers)
        except Exception:  # noqa: BLE001
            return True

    def _check_covers_load(self) -> bool:
        """Whether the answer check applies to what is loaded: one llama.cpp model
        on this machine. A managed split, a pipeline or whole-model load and a load
        with rpc_servers are not checked (see services/acceptance.py)."""
        return (self._pipeline is None and not self._loaded_rpc_servers
                and not _native_model(self._model))

    def acceptance_summary(self) -> dict:
        """The answer-check result for what is loaded, as /health/inference reports it."""
        if not self.is_loaded():
            return acceptance.summary()
        if getattr(self._model, "managed_native", False):
            return acceptance.summary(model_id=self._model_id,
                                      message="Native local engine qualification is pending. Use it on your own machines; lending is paused.")
        if not self._check_covers_load():
            return acceptance.summary(model_id=self._model_id)
        if not self._checking_answers():
            return acceptance.summary(status=acceptance.SKIPPED, model_id=self._model_id)
        return dict(self._acceptance) if self._acceptance is not None else acceptance.summary(model_id=self._model_id)

    def answers_verified(self) -> bool | None:
        """True when the loaded model passed its answer check, False when it did
        not, was not checked, or the owner turned the check off. None when nothing
        is loaded, and when the check does not cover this kind of load."""
        if not self.is_loaded():
            return None
        if getattr(self._model, "managed_native", False):
            return False
        if not self._check_covers_load():
            return None
        return bool(self._acceptance and self._acceptance["status"] == acceptance.PASSED and self._checking_answers())

    async def accept_loaded_async(
        self, *, expected_model: str | None, expected_sha256: str | None, level: str, force: bool
    ) -> tuple[dict, int | None]:
        """Check the loaded model's answers now, or reuse its proof, unless force.
        Returns the result and the tokens the checks generated. May move the model
        to the CPU, so it takes the same gates as a load."""
        if self._model is None:
            raise RuntimeError("No model loaded")
        return await self._run_lifecycle(
            partial(self._accept_loaded, expected_model, expected_sha256, level, force)
        )

    def _accept_loaded(self, expected_model, expected_sha256, level, force) -> tuple[dict, int | None]:
        self.assert_model_identity(expected_model, expected_sha256)
        if getattr(self._model, "managed_native", False):
            raise RuntimeError("Answer checks are not available for the native local backend yet. Use llama-cpp-python to run this check.")
        if self._model is None or _native_model(self._model):
            raise RuntimeError("No model loaded")
        self._check_loaded(level, force=force)
        return self.acceptance_summary(), self._acceptance_tokens

    def _load_with_check(self, model_path: str, *, verify: bool, **load_kwargs) -> dict:
        """load_model, then the answer check, as one lifecycle step."""
        if load_kwargs.get("rpc_servers"):
            # The weights are spread over other machines: there is no single
            # device to cross-check, so this load is not verified.
            result = self.load_model(model_path, **load_kwargs)
            self._acceptance = acceptance.summary(model_id=self._model_id)
            return result
        args = {"model_path": model_path, **load_kwargs}
        known_bad = self._known_bad_gpu(args) if verify else None
        result = self.load_model(model_path, **load_kwargs, **({"cpu_only": True} if known_bad else {}))
        self._load_args = args
        if not verify:
            self._acceptance = acceptance.summary(status=acceptance.SKIPPED, model_id=self._model_id)
            return result
        try:
            # Only the required checks run here. The recorded ones follow in the background
            # (see _run_recorded), started by load_model_async once this has returned.
            result = self._check_loaded("quick", force=False, known_bad=known_bad, defer=True) or result
        except Exception as exc:  # noqa: BLE001 - a fault in the check must not undo a good load
            if self._model is None:
                raise  # a reload inside the check failed and left nothing loaded: the load failed
            logger.error("answer_check_error model_id=%s error=%s", self._model_id, type(exc).__name__)
            self._stop_recorded("the check failed")
            self._acceptance = acceptance.summary(model_id=self._model_id)
        return result

    def _check_loaded(
        self, level: str, *, force: bool, known_bad: dict | None = None, defer: bool = False,
    ) -> dict | None:
        """Prove the answers of what is loaded, or reuse a proof of them.

        When the checks fail on a load with layers on a GPU, the same model is
        loaded on the CPU and checked again. Right there and wrong on the GPU
        means the GPU is the problem and the model stays on the CPU. Wrong on
        both means the model is the problem, and the planned (faster) load is
        put back. Returns the result of the last load when this moved the model.

        defer: a load. The verdict is decided by the required checks alone, and
        the recorded ones are left for the background (acceptance.RecordedPlan,
        _run_recorded). Without it (an explicit test) every check of the level
        runs before this returns, and a stored result that lacks recorded checks
        gets them now.
        """
        args = self._load_args
        store = acceptance.get_store()
        sha = self.current_model_sha256()
        moved: dict | None = None
        self._stop_recorded("superseded")  # a check of the same model that is still filling in is replaced by this one

        if force and args and (self._acceptance or {}).get("fallback"):
            # Asking again from a CPU fallback asks the GPU again: the driver may be fixed.
            moved = self._reload(args)
            known_bad = None

        key = self._key_now(level, sha)
        if known_bad is None and not force and key:
            proof = store.get(key)
            if proof and proof.get("verdict") == acceptance.OK:
                self._publish_stored(level, key, proof, defer=defer)
                return moved
            if proof and proof.get("verdict") == acceptance.MODEL_FAILED:
                # Failed on this setup within the last week, on the CPU as well as the GPU
                # if it had one. The planned load stays as it is.
                self._publish(level, key, proof=proof, status=acceptance.FAILED, verdict=acceptance.MODEL_FAILED,
                              message=acceptance.MODEL_FAILED_TEXT)
                self._remember_measurement(key, proof)
                return moved
            if proof and proof.get("verdict") == acceptance.GPU_WRONG_OUTPUT and self._gpu_offload_active() and args:
                known_bad = proof  # loaded before this was known: move it now

        gpu_run = gpu_measured = None
        if known_bad is None:
            run = self._run_checks_now(level, defer=defer)
            measured = self._measurement_now()  # before any reload replaces the window
            if run.passed:
                self._store(key, run, acceptance.OK, measurement=measured)
                self._publish(level, key, run=run)
                return moved
            if not self._gpu_offload_active() or not args:
                self._store(key, run, acceptance.MODEL_FAILED, measurement=measured)
                self._publish(level, key, run=run, status=acceptance.FAILED, verdict=acceptance.MODEL_FAILED,
                              message=acceptance.MODEL_FAILED_TEXT)
                return moved
            gpu_run, gpu_measured = run, measured
        gpu_key = known_bad["key"] if known_bad is not None else key

        if not (self._load_report or {}).get("cpu_only"):
            try:
                moved = self._reload(args, cpu_only=True)
            except Exception as exc:  # noqa: BLE001 - e.g. not enough memory for a second copy
                logger.error("answer_check_cpu_load_failed model_id=%s error=%s", self._model_id, type(exc).__name__)
                # That attempt freed the first load. Put it back, so the engine says what it has.
                moved = self._reload(args)
                shown = gpu_run.api() if gpu_run else acceptance.public_checks((known_bad or {}).get("checks", []))
                self._publish(level, self._key_now(level, sha), checks=shown, status=acceptance.FAILED,
                              verdict=acceptance.MODEL_FAILED, message=acceptance.MODEL_FAILED_TEXT)
                return moved

        cpu_key = self._key_now(level, sha)
        cpu_proof = store.get(cpu_key) if (cpu_key and known_bad is not None and not force) else None
        if cpu_proof and cpu_proof.get("verdict") == acceptance.OK:
            self._publish_stored(level, cpu_key, cpu_proof, defer=defer, verdict=acceptance.GPU_WRONG_OUTPUT,
                                 fallback=acceptance.FALLBACK_TO_CPU, message=acceptance.GPU_WRONG_TEXT)
            return moved
        cpu_run = self._run_checks_now(level, defer=defer)
        cpu_measured = self._measurement_now()
        if cpu_run.passed:
            if gpu_run is not None:
                self._store(gpu_key, gpu_run, acceptance.GPU_WRONG_OUTPUT, cross_check={"device": "CPU", "passed": True},
                            measurement=gpu_measured)
            self._store(cpu_key, cpu_run, acceptance.OK, measurement=cpu_measured)
            self._publish(level, cpu_key, run=cpu_run, verdict=acceptance.GPU_WRONG_OUTPUT,
                          fallback=acceptance.FALLBACK_TO_CPU, message=acceptance.GPU_WRONG_TEXT)
            return moved

        # Wrong on the CPU too: the model is the problem, not the GPU. The GPU is
        # faster, so the planned load goes back.
        cross_check = {"device": "CPU", "passed": False}
        self._store(cpu_key, cpu_run, acceptance.MODEL_FAILED, measurement=cpu_measured)
        # The GPU key holds the verdict for the load that is put back. When it
        # held "the GPU is wrong", the CPU now says otherwise and this replaces it.
        self._store(gpu_key, gpu_run or cpu_run, acceptance.MODEL_FAILED, cross_check=cross_check,
                    measurement=gpu_measured)
        moved = self._reload(args)
        self._publish(level, self._key_now(level, sha), run=gpu_run or cpu_run, status=acceptance.FAILED,
                      verdict=acceptance.MODEL_FAILED, message=acceptance.MODEL_FAILED_TEXT)
        return moved

    def _reload(self, args: dict, *, cpu_only: bool = False) -> dict:
        """Load the same model again with the arguments of the original load."""
        extra = {"cpu_only": True} if cpu_only else {}
        result = self.load_model(
            args["model_path"], n_ctx=args["n_ctx"], n_gpu_layers=args["n_gpu_layers"],
            n_threads=args["n_threads"], rpc_servers=None, **extra,
        )
        self._load_args = args  # the release inside load_model forgot it
        return result

    def _run_checks_now(self, level: str, *, defer: bool = False) -> acceptance.CheckRun:
        before = self._acceptance
        self._acceptance = acceptance.summary(
            status=acceptance.RUNNING, model_id=self._model_id, level=level, ran_on=self._ran_on(None),
        )
        # The checks belong to the load's memory window: a long prompt can need buffers
        # the load did not. Without a window (nothing measured the load) they add none.
        # With defer the window covers the required checks, which include the first answer
        # (that is when weights not yet read from disk are paged in). The recorded checks
        # are measured one by one when they run (memory_probe.Increment) and widen the figure.
        sampler = self._open_memory_window_over(self._load_memory)
        try:
            return acceptance.run_checks(
                self._model, level, n_ctx=self._loaded_n_ctx, on_cpu=not self._gpu_offload_active(),
                defer_recorded=defer,
            )
        except BaseException:
            self._acceptance = before  # "running" must not outlive the checks
            raise
        finally:
            if sampler is not None:
                self._load_memory = {**sampler.stop(), "window": "load+required" if defer else "load+checks"}

    def _open_memory_window_over(self, window: dict | None):
        if window is None:
            return None
        try:
            return memory_probe.MemorySampler(previous=window, gpu=self._gpu_meter).start()
        except Exception:  # noqa: BLE001
            logger.debug("memory_window_not_extended", exc_info=True)
            return None

    def _measurement_now(self) -> dict | None:
        """What loading (and, once they ran, checking) the current model cost, with the context size
        and layer split it was measured at. None when nothing was measured."""
        window = self._load_memory
        if not window:
            return None
        report = self._load_report or {}
        return {**window, "n_ctx": self._loaded_n_ctx, "n_gpu_layers": report.get("used_n_gpu_layers"),
                "offloaded_layers": report.get("offloaded_layers"), "total_layers": report.get("total_layers"),
                "measured_at": acceptance.iso()}

    def _loaded_file(self) -> dict | None:
        return model_evidence.file_identity(self._model_path, self._model_id) if self._model_path else None

    def _remember_measurement(self, key: dict | None, proof: dict | None) -> None:
        """A reused proof keeps the measurement it was stored with. One stored before measurements
        existed gets this load's reading, once, so the model is measured without being checked again."""
        if key is None or proof is None:
            return
        fields = {}
        if "model" not in proof and (identity := self._loaded_file()):
            fields["model"] = identity
        if "measurement" not in proof and (measured := self._measurement_now()):
            fields["measurement"] = measured
        if fields:
            acceptance.get_store().attach(key, fields)

    def _store(self, key: dict | None, run: acceptance.CheckRun, verdict: str, *,
               measurement: dict | None = None, **extra) -> None:
        if key is not None:
            record = {"verdict": verdict, "level": run.level, "duration_ms": run.duration_ms,
                      "output_tokens": run.output_tokens, "checks": run.records(), **extra}
            if measurement is not None:
                record["measurement"] = measurement
            if (identity := self._loaded_file()) is not None:
                record["model"] = identity
            acceptance.get_store().put(key, record)

    def _publish(
        self, level: str, key: dict | None, *, run: acceptance.CheckRun | None = None, proof: dict | None = None,
        checks: list[dict] | None = None, status: str = acceptance.PASSED, verdict: str = acceptance.OK,
        fallback: dict | None = None, message: str | None = None, plan: acceptance.RecordedPlan | None = None,
    ) -> None:
        """Publish the result. `plan` (or the `pending` plan of `run`) holds the recorded checks still to
        run: the result then says `recorded: "running"` and a job is made for them, which load_model_async
        starts. Without a plan, `recorded` says whether the result already holds all of them."""
        tokens = None
        if run is not None:
            checks, checked_at, tokens = run.api(), acceptance.iso(), run.output_tokens
            plan = plan or run.pending
            recorded = acceptance.STOPPED if run.deferred else acceptance.DONE
        elif proof is not None:
            checks = acceptance.public_checks(proof.get("checks", []))
            checked_at, tokens = proof.get("checked_at"), proof.get("output_tokens")
            recorded = acceptance.DONE if acceptance.recorded_done(proof.get("checks")) else acceptance.STOPPED
        else:
            checked_at = acceptance.iso()
            recorded = acceptance.STOPPED
        self._recorded_job = None
        if plan is not None and status == acceptance.PASSED and self._model is not None:
            recorded = acceptance.RUNNING
            self._recorded_job = _RecordedJob(
                model=self._model, model_id=self._model_id, key=key, level=level, plan=plan,
                base_checks=list(checks or []), from_proof=run is None,
            )
        self._acceptance_tokens = tokens
        self._acceptance = acceptance.summary(
            status=status, model_id=self._model_id, level=level, checked_at=checked_at,
            from_proof=run is None and proof is not None, ran_on=self._ran_on(key), checks=checks,
            verdict=verdict, fallback=fallback, message=message, recorded=recorded,
        )
        logger.info(
            "answer_check status=%s verdict=%s level=%s from_proof=%s fallback=%s checks_passed=%d/%d model_id=%s",
            status, verdict, level, proof is not None and run is None, fallback is not None,
            sum(1 for c in checks or [] if c["passed"]), len(checks or []), self._model_id,
        )

    def _ran_on(self, key: dict | None) -> dict:
        summary = self.inference_summary() or {}
        return {
            "device_kind": summary.get("device_kind", "unknown"),
            "backend": summary.get("backend", "unknown"),
            "device_name": summary.get("device_name", "unknown"),
            "driver": (key or {}).get("driver"),
        }

    def _key_now(self, level: str, sha: str | None) -> dict | None:
        """The proof key of what is loaded right now. None when the file's hash is unknown."""
        if not sha or not self._model_path:
            return None
        report = self._load_report or {}
        return self._key_for(
            sha, self._model_path, report.get("used_n_gpu_layers", 0), cpu_only=bool(report.get("cpu_only")),
            level=level, device=report.get("offload_device"),
        )

    @staticmethod
    def _key_for(sha: str, model_path: str, n_gpu_layers: int, *, cpu_only: bool, level: str,
                 device: dict | None = None) -> dict:
        from potluck_local.services import llama_backend
        from potluck_local.services.driver_fingerprint import driver_fingerprint

        selection = llama_backend.current_selection()
        if n_gpu_layers == 0:
            kind, name = "cpu", "CPU"
        else:
            kind = "gpu" if n_gpu_layers < 0 else "mixed"
            if device is None:
                chosen = llama_backend.offload_device(llama_backend.describe_devices() or selection.devices)
                device = chosen[1] if chosen else {}
            name = device.get("description") or device.get("name") or "unknown"
        return acceptance.build_key(
            sha256=sha, backend=selection.backend, device=name, device_kind=kind, n_gpu_layers=n_gpu_layers,
            total_layers=llama_backend.read_gguf_layer_count(model_path), cpu_only=cpu_only,
            driver=driver_fingerprint(None if kind == "cpu" else name),
            library=llama_backend.library_signature(), level=level,
        )

    def _known_bad_gpu(self, args: dict) -> dict | None:
        """A stored proof that the GPU this load is about to use gives wrong
        answers, so the load can start on the CPU instead of waiting on a GPU
        known to be wrong. A machine with no such proof pays for one file read."""
        store = acceptance.get_store()
        try:
            if not store.holds_gpu_wrong_output():
                return None
            from potluck_local.services import llama_backend

            path = args["model_path"]
            # Mirrors the plan load_model is about to make with the same inputs.
            selection = llama_backend.current_selection()
            layers = args["n_gpu_layers"]
            if selection.backend == "cpu":
                layers = 0
            elif selection.probed:
                layers = llama_backend.plan_gpu_layers(
                    path, requested=layers, n_ctx=args["n_ctx"], devices=llama_backend.describe_devices(),
                ).n_gpu_layers
            if layers == 0:
                return None
            sha = (self.current_model_sha256() if self._model_path == path else None) or artifact_identity(Path(path))[0]
            proof = store.get(self._key_for(sha, path, layers, cpu_only=False, level="quick"))
            return proof if proof and proof.get("verdict") == acceptance.GPU_WRONG_OUTPUT else None
        except Exception as exc:  # noqa: BLE001 - the lookup only saves time
            logger.debug("answer_check_lookup_failed error=%s", type(exc).__name__)
            return None

    # -----------------------------------------------------------------
    # Recorded answer checks, after the verdict.
    #
    # The required checks decide the verdict and run inside the load. The
    # recorded ones (acceptance.RECORDED_CHECKS) only add to the result, and the
    # reasoning one can generate for 512 tokens, so they run afterwards, from
    # _run_recorded, under these rules:
    #   - Each takes _inference_lock the way a request does, so it never runs
    #     at the same time as one, nor while a load or unload is swapping the model.
    #   - Only one at a time, and a check starts only when nothing holds or
    #     waits for either gate. A request is never queued behind a check that
    #     has not started.
    #   - A request that arrives while a check is generating sets the job's
    #     interrupt. The check reads its reply from a stream and stops at the
    #     next token, recorded as skipped with the reason "the machine was busy".
    #     Closing a stream early is what a disconnecting client does, so the
    #     model is left as it is then. Prompt evaluation before the first token
    #     cannot be stopped; for the long-prompt check that is the wait.
    #   - Unload, a model swap, a new check of the same model and the engine
    #     stopping all end the job. Nothing touches the model after that.

    def _publish_stored(self, level: str, key: dict | None, proof: dict, *, defer: bool, **publish) -> None:
        """Publish a stored passing result. One that lacks recorded checks gets them: in the background
        after a load (the verdict does not wait for them), or now for an explicit test, which returns
        the whole result."""
        self._remember_measurement(key, proof)
        plan = self._missing_plan(level, proof)
        if plan is not None and not defer:
            proof, plan = self._run_missing_now(key, proof, plan), None
        self._publish(level, key, proof=proof, plan=plan, **publish)

    def _missing_plan(self, level: str, proof: dict) -> acceptance.RecordedPlan | None:
        if self._model is None or _native_model(self._model):
            return None
        return acceptance.resume_plan(
            self._model, level, n_ctx=self._loaded_n_ctx, on_cpu=not self._gpu_offload_active(),
            records=proof.get("checks"),
        )

    def _run_missing_now(self, key: dict | None, proof: dict, plan: acceptance.RecordedPlan) -> dict:
        outcomes: list[acceptance.Outcome] = []
        increment = self._start_increment()
        try:
            for step_id in plan.steps:
                outcome = plan.run_step(step_id)
                if outcome is not None:
                    outcomes.append(outcome)
        finally:
            measured = self._stop_increment(increment)
        increments = [measured] if measured else []
        self._widen_load_memory(increments)
        self._save_recorded(key, outcomes, increments)
        return {**proof, "checks": acceptance.merge_records(proof.get("checks"), [o.record() for o in outcomes]),
                "output_tokens": int(proof.get("output_tokens") or 0) + sum(o.tokens for o in outcomes)}

    def _start_increment(self):
        """A memory reading for work done after the load's window closed, or None when the load was not measured."""
        if self._load_memory is None:
            return None
        try:
            return memory_probe.Increment(gpu=self._gpu_meter).start()
        except Exception:  # noqa: BLE001 - a measurement must never stop a check
            logger.debug("memory_increment_not_started", exc_info=True)
            return None

    @staticmethod
    def _stop_increment(increment) -> dict | None:
        if increment is None:
            return None
        try:
            return increment.stop()
        except Exception:  # noqa: BLE001
            logger.debug("memory_increment_not_stopped", exc_info=True)
            return None

    def _widen_load_memory(self, increments: list[dict]) -> None:
        if increments and self._load_memory is not None:
            self._load_memory = memory_probe.widen(self._load_memory, increments)

    @staticmethod
    def _save_recorded(key: dict | None, outcomes: list[acceptance.Outcome], increments: list[dict]) -> bool:
        """Add finished recorded checks, and what they cost, to the stored proof."""
        if key is None or not (outcomes or increments):
            return False
        return acceptance.get_store().extend(
            key, [o.record() for o in outcomes],
            duration_ms=sum(o.ms for o in outcomes), output_tokens=sum(o.tokens for o in outcomes),
            widen=(lambda measurement: memory_probe.widen(measurement, increments)) if increments else None,
        )

    def _stop_recorded(self, reason: str, *, wait: bool = False) -> None:
        """End the recorded checks of the loaded model. With `wait`, also wait for the one that is generating."""
        job, self._recorded_job = self._recorded_job, None
        if job is None:
            return
        job.stop(reason)
        self._wake_recorded_threadsafe()
        if wait and not job.idle.is_set():
            # The interrupt makes the running check return at its next token. The bound is for a
            # prompt that takes long to evaluate.
            job.idle.wait(timeout=120)

    def _request_is_waiting(self) -> None:
        """Called on the event loop when a caller is about to wait for the inference gate."""
        job = self._recorded_job
        if self._background_holds and job is not None:
            job.interrupt.set()

    def _wake_recorded(self) -> None:
        watchers, self._recorded_watchers = self._recorded_watchers, []
        for future in watchers:
            if not future.done():
                future.set_result(None)

    def _wake_recorded_threadsafe(self) -> None:
        loop = self._recorded_loop
        if loop is not None and self._recorded_watchers:
            try:
                loop.call_soon_threadsafe(self._wake_recorded)
            except RuntimeError:  # the loop is closed: nobody is waiting
                pass

    def _launch_recorded(self) -> None:
        """Start the recorded checks a load left to run. Called on the event loop, after the load has
        returned and released both gates, so a request that was waiting for them is served first."""
        job = self._recorded_job
        if job is None or job.started or job.stopped is not None:
            return
        job.started = True
        self._recorded_loop = asyncio.get_running_loop()
        self._recorded_task = self._recorded_loop.create_task(self._run_recorded(job))

    async def wait_for_recorded(self) -> None:
        """Wait until the recorded checks of the loaded model have ended (done or stopped)."""
        task = self._recorded_task
        if task is not None and not task.done():
            await asyncio.shield(task)

    def _recorded_runnable(self, job: _RecordedJob) -> bool:
        if job.stopped is None:
            if not self._checking_answers():
                job.stop("the check was turned off")
            elif self._recorded_job is not job or self._model is not job.model:
                job.stop("the model changed")
        return job.stopped is None

    def _model_is_quiet(self) -> bool:
        gates = (self._admission_lock, self._inference_lock)
        return not any(g.locked() or g.waiting for g in gates)

    async def _take_model_for(self, job: _RecordedJob) -> bool:
        """Wait until no request holds or waits for the model, then take the inference gate. False when the
        job ended while waiting. The check and the take are one step on the event loop: nothing can queue
        between them, and a free gate is taken without waiting."""
        while True:
            # Let what is already scheduled on the loop run first: a request on its way queues now,
            # before this looks at the gates.
            await asyncio.sleep(0)
            if not self._recorded_runnable(job):
                return False
            if self._model_is_quiet():
                await self._inference_lock.acquire()
                self._background_holds = True
                job.interrupt.clear()
                return True
            future = asyncio.get_running_loop().create_future()
            self._recorded_watchers.append(future)
            try:
                await future
            finally:
                if future in self._recorded_watchers:
                    self._recorded_watchers.remove(future)

    def _recorded_step(self, job: _RecordedJob, step_id: str) -> tuple[acceptance.Outcome | None, dict | None]:
        """One recorded check, on a worker thread, with its own memory reading. `job.idle` was cleared
        before this was submitted and is set when the model is no longer in use here."""
        try:
            if job.stopped is not None:
                return None, None  # stopped between the take and the start: the model may be on its way out
            increment = self._start_increment()
            try:
                outcome = job.plan.run_step(step_id, job.interrupt)
            finally:
                measured = self._stop_increment(increment)
            return outcome, measured
        finally:
            job.idle.set()

    async def _run_recorded(self, job: _RecordedJob) -> None:
        try:
            for step_id in list(job.plan.steps):
                for attempt in range(RECORDED_TRIES):
                    taken = await self._run_step_once(job, step_id)
                    if taken is None:
                        return  # the job ended (finally below says how)
                    outcome, measured = taken
                    if measured is not None:
                        job.increments.append(measured)  # what an attempt used counts, whether it finished or not
                    busy = outcome is not None and outcome.skipped == acceptance.BUSY
                    if busy and attempt + 1 < RECORDED_TRIES and self._recorded_runnable(job):
                        continue  # a request took the model; once it has gone, ask again
                    self._apply_step(job, step_id, outcome)
                    break
        except asyncio.CancelledError:
            job.stop("the engine is stopping")
            raise
        except Exception as exc:  # noqa: BLE001 - the verdict stands; a fault here ends the extra checks only
            job.stop(f"stopped by {type(exc).__name__}")
            logger.warning("answer_check_recorded_error model_id=%s error=%s category=%s", job.model_id,
                           type(exc).__name__, getattr(exc, "category", None) or "unknown")
        finally:
            await self._finish_recorded(job)

    async def _run_step_once(self, job: _RecordedJob, step_id: str) -> tuple[acceptance.Outcome | None, dict | None] | None:
        """Wait for a quiet model, run one recorded check on it, and give the model back. None when the
        job ended while waiting."""
        if not await self._take_model_for(job):
            return None
        loop = asyncio.get_running_loop()
        try:
            job.idle.clear()
            job.attempts += 1
            try:
                worker = loop.run_in_executor(None, partial(self._recorded_step, job, step_id))
            except BaseException:
                job.idle.set()  # nothing was started: nobody has to be waited for
                raise
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                job.stop("the engine is stopping")
                await self._drain_worker(worker)
                raise
        finally:
            self._background_holds = False
            self._inference_lock.release()

    def _apply_step(self, job: _RecordedJob, step_id: str, outcome: acceptance.Outcome | None) -> None:
        """Take a finished check into the job and, while the job is still the current one, into the result."""
        job.finished.append(step_id)
        if outcome is not None:
            job.outcomes.append(outcome)
        self._publish_progress(job)

    def _publish_progress(self, job: _RecordedJob, recorded: str | None = None) -> None:
        if self._recorded_job is not job and recorded is None:
            return
        current = self._acceptance
        if current is None or current.get("model_id") != job.model_id or self._model is not job.model:
            return
        checks = acceptance.merge_records(job.base_checks, [o.api() for o in job.outcomes])
        self._acceptance = {**current, "checks": checks, "recorded": recorded or current.get("recorded")}

    async def _finish_recorded(self, job: _RecordedJob) -> None:
        """The recorded checks ended. Store what they found, then say how it ended. Never raises."""
        loop = asyncio.get_running_loop()
        done = job.stopped is None and len(job.finished) == len(job.plan.steps)
        state = acceptance.DONE if done else acceptance.STOPPED
        try:
            if job.stopped != "superseded":
                # A newer check of this model has its own result. Anything else keeps what finished.
                if self._model is job.model:
                    self._widen_load_memory(job.increments)
                await asyncio.shield(loop.run_in_executor(None, partial(
                    self._save_recorded, job.key, job.outcomes, job.increments)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("answer_check_recorded_not_saved model_id=%s error=%s", job.model_id, type(exc).__name__)
        finally:
            if self._recorded_job is job:
                self._recorded_job = None
            if job.stopped != "superseded":
                self._publish_progress(job, recorded=state)
            ran = [o for o in job.outcomes if not o.skipped]
            logger.info(
                "answer_check_recorded status=%s reason=%s checks_passed=%d/%d ran=%d skipped=%d busy=%d "
                "memory_measured=%d/%d from_proof=%s duration_ms=%d model_id=%s",
                state, job.stopped or "finished", sum(1 for o in job.outcomes if o.passed), len(job.outcomes),
                len(ran), len(job.outcomes) - len(ran), sum(1 for o in job.outcomes if o.skipped == acceptance.BUSY),
                len(job.increments), job.attempts, job.from_proof,
                round((time.monotonic() - job.started_at) * 1000), job.model_id,
            )

    async def _run_lifecycle(self, operation):
        async with self._admission_lock, self._inference_lock:
            worker = asyncio.get_running_loop().run_in_executor(None, operation)
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling the HTTP request cannot stop a native load/close.
                # Keep both gates until it has actually finished touching the model.
                await self._drain_worker(worker)
                raise

    @staticmethod
    async def _drain_worker(worker):
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue  # repeated cancellation must not release model access early
            except Exception:
                break
        # Retrieve any worker exception even when cancellation won the race.
        if not worker.cancelled():
            worker.exception()

    async def _cancel_split_request(self, model, cancel_event):
        # Keep model access until cancellation finishes, even if the caller is
        # cancelled again while the native engine is acknowledging the stop.
        cancel_event.set()
        worker = asyncio.create_task(asyncio.to_thread(model.cancel, cancel_event))
        await self._drain_worker(worker)

    @asynccontextmanager
    async def _stream_session(self):
        """First batched stream acquires exclusive access; the last releases it."""
        async with self._admission_lock:
            if self._batched_streams == 0:
                await self._inference_lock.acquire()
            batched = self._batcher is not None
            if batched:
                self._batched_streams += 1
        try:
            yield batched
        finally:
            # Event-loop-only bookkeeping, deliberately without an await: an
            # unload may already hold the admission gate while waiting for us.
            if batched:
                self._batched_streams -= 1
            if self._batched_streams == 0:
                self._inference_lock.release()

    def _release_model(self) -> None:
        """Inner release — must be called while _load_lock is held."""
        self._artifact = None
        self._verified_artifact = None
        # Recorded checks of the model going away stop here. One that is generating is told to stop
        # and waited for, so the model is never freed under it (only a synchronous unload can get
        # here while one runs: the lifecycle operations hold the gate the checks run under).
        self._stop_recorded("the model was unloaded", wait=True)
        self._acceptance = None
        self._acceptance_tokens = None
        self._load_args = None
        self._load_memory = None
        self._gpu_meter = None
        self._persisted_benchmarks = None
        if self._model is not None:
            import gc

            model_id = self._model_id
            # Drop the Llama handle explicitly so ggml-metal frees device
            # resources now, not during interpreter atexit (which can
            # GGML_ASSERT on macOS and pop the system "Reopen?" crash dialog).
            model = self._model
            self._model = None
            self._model_path = None
            self._model_id = None
            self._loaded_n_ctx = None
            self._loaded_rpc_servers = None
            self._load_report = None
            self._detach_prompt_cache(model)
            close = getattr(model, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    logger.exception("model_close_failed model_id=%s", model_id)
            del model
            gc.collect()
            logger.info("model_unloaded model_id=%s", model_id)
        if self._batcher is not None:
            self._batcher.stop()
            self._batcher = None
        if self._pipeline is not None:
            model_id = self._model_id
            self._pipeline = None  # drops the head block + tokenizer; torch frees on GC
            self._model_id = None
            logger.info("pipeline_unloaded model_id=%s", model_id)

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        expected_model: str | None = None,
        expected_sha256: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stop: list[str] | None = None,
        top_p: float = 1.0,
        top_k: int = 0,
        seed: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        tool_proof: bool = False,
    ):
        """Yield OpenAI SSE with shared admission only for continuous batchers.

        Select the backend after admission, so a request queued behind a model
        swap uses the newly loaded backend. Sampling stays per request; the
        llama.cpp path retains its existing temperature/stop defaults.
        """
        async with self._stream_session() as batched:
            self.assert_model_identity(expected_model, expected_sha256)
            cancel_event = threading.Event()
            cancel_model = None
            sampling = dict(max_new=max_tokens, temperature=temperature,
                            top_k=top_k, top_p=top_p, seed=seed)
            if self._batcher is not None:
                produce = partial(self._batcher.submit, messages, **sampling)
                label = f"cb_stream model_id={self._model_id}"
            elif self._pipeline is not None:
                produce = partial(self._pipeline.stream_tokens, messages, **sampling)
                label = f"pipeline_stream model_id={self._model_id}"
            elif self._model is not None:
                model = self._model
                if _native_model(model):
                    cancel_model = model
                label = f"stream model_id={self._model_id}"

                def produce():
                    # Control-token text in the request stays text (prompt_guard).
                    safe_messages, extra, ticket = prompt_guard.protect_chat(
                        model, messages, tool_calls.completion_kwargs(tools, tool_choice))
                    chunks = None
                    try:
                        # tool_proof: mark the tool-call markers the model sampled as
                        # the marker token (tool_markers).
                        with tool_markers.watch(model, tool_proof and bool(extra.get("tools"))) as reader:
                            chunks = model.create_chat_completion(
                                messages=safe_messages, temperature=temperature, max_tokens=max_tokens,
                                stop=stop or [], stream=True, **extra,
                                **({"cancel_event": cancel_event} if cancel_model is not None else {}),
                            )
                            for chunk in chunks:
                                piece = chunk["choices"][0].get("delta", {}).get("content")
                                yield reader.text(piece) if reader is not None and piece else piece
                            if reader is not None:
                                yield reader.flush()
                    finally:
                        close = getattr(chunks, "close", None)
                        if close is not None:
                            close()
                        ticket.close()
            else:
                yield 'data: {"error":"No model loaded"}\n\n'
                yield "data: [DONE]\n\n"
                return

            # Explicitly close the nested generator before releasing model access
            # on disconnect. An async-for alone leaves cleanup to finalization.
            async with aclosing(self._stream_sse_from_blocking(
                produce, label=label, drain_on_cancel=batched,
                cancel_event=cancel_event, cancel_model=cancel_model,
            )) as stream:
                async for chunk in stream:
                    yield chunk

    async def _stream_sse_from_blocking(
        self, produce, *, label, drain_on_cancel=False, cancel_event=None, cancel_model=None,
    ):
        """Pump a blocking iterator into SSE; caller must hold _stream_session.

        Batchers own their decode loop and currently have no per-row cancel API.
        On disconnect, discard that row's output and drain to its terminal event
        before releasing model access. Other rows can continue independently.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        if cancel_event is None:
            cancel_event = threading.Event()

        def _generate() -> None:
            chunks = None
            try:
                chunks = produce()
                for piece in chunks:
                    if cancel_event.is_set():
                        if drain_on_cancel:
                            continue
                        break
                    if piece:  # skip empty / None deltas
                        loop.call_soon_threadsafe(queue.put_nowait, piece)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as exc:
                failure = inference_errors.describe_failure(exc, gpu_offload=self._gpu_offload_active())
                if not cancel_event.is_set():
                    logger.error("%s error=%s category=%s", label, type(exc).__name__, failure.category)
                loop.call_soon_threadsafe(queue.put_nowait, failure)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()

        fut = loop.run_in_executor(None, prompt_cache.in_request_context(_generate))
        token_count = 0
        completed = False
        start = time.monotonic()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    completed = True
                    break
                if isinstance(item, inference_errors.FailureDescription):
                    # "error" stays a string for the clients that read it as one;
                    # "category" is the stable code. Routes that serve another
                    # machine choose their own text from the category.
                    yield f'data: {json.dumps({"error": item.message, "category": item.category})}\n\n'
                    break
                token_count += 1
                yield f'data: {json.dumps({"choices": [{"delta": {"content": item}}]})}\n\n'
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.info("%s cancelled tokens_so_far=%d", label, token_count)
        finally:
            cancel_event.set()
            if cancel_model is not None and not completed:
                await self._cancel_split_request(cancel_model, cancel_event)
            await self._drain_worker(fut)
            logger.info("%s complete tokens=%d duration=%.3fs",
                        label, token_count, round(time.monotonic() - start, 3))

    async def chat(
        self,
        messages: list[dict],
        *,
        expected_model: str | None = None,
        expected_sha256: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        stop: list[str] | None = None,
        tool_proof: bool = False,
    ) -> dict:
        """Non-streaming completion. Returns raw llama result dict."""
        if self._pipeline is not None:
            raise RuntimeError(
                "pipeline-parallel mode supports the streaming chat path only; "
                "the raw / non-streaming path is a follow-on"
            )
        if self._model is None:
            raise RuntimeError("No model loaded")

        async with self._inference_lock:
            self.assert_model_identity(expected_model, expected_sha256)
            loop = asyncio.get_running_loop()
            model = self._model
            model_id = self._model_id
            start = time.monotonic()
            managed_split = _native_model(model)
            cancel_event = threading.Event()

            def _complete():
                # Control-token text in the request stays text (prompt_guard).
                safe_messages, extra, ticket = prompt_guard.protect_chat(
                    model, messages, tool_calls.completion_kwargs(tools, tool_choice))
                try:
                    with tool_markers.watch(model, tool_proof and bool(extra.get("tools"))) as reader:
                        result = model.create_chat_completion(
                            messages=safe_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            stream=False,
                            **({"stop": stop} if stop else {}),
                            **extra,
                            **({"cancel_event": cancel_event} if managed_split else {}),
                        )
                        if reader is not None:
                            _mark_reply(reader, result)
                        return result
                finally:
                    ticket.close()

            fut = loop.run_in_executor(None, prompt_cache.in_request_context(_complete))
            try:
                result = await asyncio.shield(fut)
            except asyncio.CancelledError:
                if managed_split:
                    await self._cancel_split_request(model, cancel_event)
                raise
            except Exception as exc:
                failure = inference_errors.describe_failure(exc, gpu_offload=self._gpu_offload_active())
                logger.error(
                    "chat_error model_id=%s error=%s category=%s", model_id, type(exc).__name__, failure.category
                )
                if failure.category == inference_errors.GPU_OUT_OF_MEMORY:
                    raise inference_errors.InferenceFailure(failure.category, failure.message) from exc
                raise
            finally:
                # A cancelled request must not free the inference lock while the
                # worker thread is still inside create_chat_completion (the model
                # is not thread-safe). Ordinary local models have no early-stop
                # hook and drain fully.
                await self._drain_worker(fut)

            elapsed = round(time.monotonic() - start, 3)
            usage = result.get("usage", {})
            logger.info(
                "chat_complete model_id=%s prompt_tokens=%s completion_tokens=%s duration=%.3fs",
                model_id,
                usage.get("prompt_tokens", "?"),
                usage.get("completion_tokens", "?"),
                elapsed,
            )
            return result

    async def stream_chat_raw(
        self,
        messages: list[dict],
        *,
        expected_model: str | None = None,
        expected_sha256: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stop: list[str] | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        tool_proof: bool = False,
    ):
        """Yield llama-cpp's native OpenAI-shaped streaming chunk dicts.

        For the OpenAI-compatible gateway, which re-emits these verbatim as SSE
        (no reshaping needed). Distinct from stream_chat(), which yields the
        simplified delta-only SSE strings the desktop UI consumes. Serialised by
        the same inference lock. An upstream error surfaces as a single dict
        {"__error__": "..."} before the stream ends.
        """
        if self._pipeline is not None:
            raise RuntimeError(
                "pipeline-parallel mode supports the streaming chat path only; "
                "the raw / non-streaming path is a follow-on"
            )
        if self._model is None:
            raise RuntimeError("No model loaded")

        async with self._inference_lock:
            self.assert_model_identity(expected_model, expected_sha256)
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            model = self._model
            model_id = self._model_id

            cancel_event = threading.Event()

            def _generate() -> None:
                chunks = None
                ticket = prompt_guard.NO_TICKET
                try:
                    # Control-token text in the request stays text (prompt_guard).
                    safe_messages, extra, ticket = prompt_guard.protect_chat(
                        model, messages, tool_calls.completion_kwargs(tools, tool_choice))
                    with tool_markers.watch(model, tool_proof and bool(extra.get("tools"))) as reader:
                        chunks = model.create_chat_completion(
                            messages=safe_messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            stop=stop or [],
                            stream=True,
                            **extra,
                            **({"cancel_event": cancel_event} if _native_model(model) else {}),
                        )
                        for chunk in chunks:
                            if cancel_event.is_set():
                                break
                            loop.call_soon_threadsafe(queue.put_nowait, chunk if reader is None else reader.chunk(chunk))
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except Exception as exc:
                    failure = inference_errors.describe_failure(exc, gpu_offload=self._gpu_offload_active())
                    if not cancel_event.is_set():
                        logger.error(
                            "gateway_stream_error model_id=%s error=%s category=%s",
                            model_id, type(exc).__name__, failure.category,
                        )
                    # "__error__" is the text for this machine's own user. A route
                    # that serves another machine must use "__category__" instead.
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        {"__error__": failure.message, "__category__": failure.category},
                    )
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                finally:
                    close = getattr(chunks, "close", None)
                    if close is not None:
                        close()
                    ticket.close()

            fut = loop.run_in_executor(None, prompt_cache.in_request_context(_generate))

            chunk_count = 0
            completed = False
            start = time.monotonic()
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        completed = True
                        break
                    chunk_count += 1
                    yield item
            except asyncio.CancelledError:
                logger.info("gateway_stream_cancelled model_id=%s chunks_so_far=%d", model_id, chunk_count)
            finally:
                # Drain the worker before releasing the inference lock (see
                # stream_chat) so a disconnect can't leave inference running on the
                # non-thread-safe model while the next request acquires the lock.
                cancel_event.set()
                if _native_model(model) and not completed:
                    await self._cancel_split_request(model, cancel_event)
                await self._drain_worker(fut)
                logger.info(
                    "gateway_stream_complete model_id=%s chunks=%d duration=%.3fs",
                    model_id, chunk_count, round(time.monotonic() - start, 3),
                )

    async def score_sequence(
        self,
        text: str,
        *,
        prefix: str | None = None,
        max_tokens: int = 2048,
    ) -> dict:
        """Teacher-forced log-probability of `text` (optionally conditional on
        `prefix`) under the currently loaded model.

        Used by closure / scoring-challenge verification (model-substitution
        detection). Two important properties:

        - Works WITHOUT loading the model with logits_all=True, so it runs on the
          normal serving model with no extra memory (logits_all would allocate
          ~n_ctx * vocab, multiple GB at serving context sizes). We force
          per-token logits for each decode batch and read them straight from the
          llama.cpp context buffer; verified to match a logits_all reference.
        - Never mutates persistent serving state: serialised by the same
          inference lock as chat, and resets + clears the KV cache before and
          after, so an in-flight/next chat is unaffected.

        Returns a dict with model_id, scored_tokens, sum_logprob,
        avg_token_logprob, bits_per_byte, n_bytes.
        """
        if self._pipeline is not None:
            raise RuntimeError(
                "pipeline-parallel mode supports the streaming chat path only; "
                "the raw / non-streaming path is a follow-on"
            )
        if self._model is None:
            raise RuntimeError("No model loaded")

        async with self._inference_lock:
            loop = asyncio.get_running_loop()
            model = self._model
            model_id = self._model_id
            if _native_model(model):
                raise RuntimeError("Scoring verification is not supported for native engine sessions.")

            def _score() -> dict:
                import math

                import numpy as np

                vocab = model._n_vocab
                n_batch = getattr(model, "n_batch", 512) or 512

                if prefix:
                    pre = list(model.tokenize(prefix.encode("utf-8"), add_bos=True, special=True))
                    cont = list(model.tokenize(text.encode("utf-8"), add_bos=False, special=False))
                    toks = pre + cont
                    start = len(pre)  # first scored token index
                else:
                    toks = list(model.tokenize(text.encode("utf-8"), add_bos=True, special=False))
                    start = 1  # skip BOS (no predictor)

                if len(toks) > max_tokens:
                    toks = toks[:max_tokens]
                start = min(start, len(toks))

                model.reset()
                model._ctx.kv_cache_clear()
                try:
                    sum_lp = 0.0
                    n = 0
                    pos = 0
                    while pos < len(toks):
                        window = toks[pos : pos + n_batch]
                        # logits_all=True for THIS batch only -> per-token logits in
                        # the C buffer; read before the next decode overwrites it.
                        model._batch.set_batch(batch=window, n_past=pos, logits_all=True)
                        model._ctx.decode(model._batch)
                        flat = np.ctypeslib.as_array(
                            model._ctx.get_logits(), shape=(len(window) * vocab,)
                        )
                        logits = flat.reshape(len(window), vocab)
                        for j in range(len(window)):
                            nxt = pos + j + 1  # token predicted by position pos+j
                            if start <= nxt < len(toks):
                                row = logits[j]
                                m = float(row.max())
                                logZ = m + float(np.log(np.exp(row - m).sum()))
                                sum_lp += float(row[toks[nxt]]) - logZ
                                n += 1
                        pos += len(window)
                finally:
                    # Leave the serving model in a clean state no matter what.
                    model.reset()
                    model._ctx.kv_cache_clear()

                nbytes = max(1, len(text.encode("utf-8")))
                return {
                    "model_id": model_id,
                    "scored_tokens": n,
                    "sum_logprob": sum_lp,
                    "avg_token_logprob": (sum_lp / n) if n else None,
                    "bits_per_byte": (-sum_lp / (math.log(2) * nbytes)) if n else None,
                    "n_bytes": nbytes,
                }

            return await loop.run_in_executor(None, _score)

    async def benchmark(
        self,
        *,
        prompt: str = "Write a short paragraph about the ocean.",
        max_tokens: int = 64,
    ) -> dict:
        """Measure real generation speed for the loaded model: time-to-first-token
        and tokens/sec over a fixed short prompt. Serialised by the inference
        lock; drains the worker on exit so it never races a chat (same pattern as
        stream_chat). Returns model_id, output_tokens, ttft_ms, duration_ms,
        tokens_per_sec."""
        if self._pipeline is not None:
            raise RuntimeError(
                "pipeline-parallel mode supports the streaming chat path only; "
                "the raw / non-streaming path is a follow-on"
            )
        if self._model is None:
            raise RuntimeError("No model loaded")

        async with self._inference_lock:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            model = self._model
            model_id = self._model_id
            if getattr(model, "managed_native", False):
                raise RuntimeError("The native local backend does not support this benchmark yet.")
            cancel_event = threading.Event()

            def _generate() -> None:
                try:
                    for chunk in model.create_chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_tokens=max_tokens,
                        stream=True,
                    ):
                        if cancel_event.is_set():
                            break
                        delta = chunk["choices"][0].get("delta", {}).get("content")
                        if delta:
                            loop.call_soon_threadsafe(queue.put_nowait, delta)
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, {"__error__": str(exc)})
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            start = time.monotonic()
            ttft: float | None = None
            n = 0
            fut = loop.run_in_executor(None, _generate)
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, dict) and "__error__" in item:
                        raise RuntimeError(item["__error__"])
                    if ttft is None:
                        ttft = time.monotonic() - start
                    n += 1
            finally:
                cancel_event.set()
                try:
                    await asyncio.shield(fut)
                except Exception:
                    pass

            total = time.monotonic() - start
            tps = (n / total) if (total > 0 and n) else 0.0

            # Prompt-processing rate: time to first token on a fixed ~700-token
            # prompt, one output token. The coordinator uses this for its
            # first-token deadline, where the short-prompt TTFT above would
            # badly overstate the rate. Best effort; None when it cannot run.
            def _measure_prompt_rate() -> float | None:
                try:
                    prompt = _PROMPT_RATE_TEXT
                    n_prompt = len(model.tokenize(prompt.encode("utf-8"), add_bos=True))
                    t0 = time.monotonic()
                    for _ in model.create_chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        max_tokens=1,
                        stream=True,
                    ):
                        break
                    dt = time.monotonic() - t0
                    if dt <= 0 or n_prompt <= 0:
                        return None
                    return round(n_prompt / dt, 1)
                except Exception:  # noqa: BLE001
                    logger.debug("benchmark_prompt_rate_failed", exc_info=True)
                    return None

            prompt_tps = await loop.run_in_executor(None, _measure_prompt_rate)

            result = {
                "model_id": model_id,
                "output_tokens": n,
                "ttft_ms": round((ttft or 0.0) * 1000, 1),
                "duration_ms": round(total * 1000, 1),
                "tokens_per_sec": round(tps, 1),
                "prompt_tokens_per_sec": prompt_tps,
            }
            logger.info(
                "benchmark model_id=%s tokens=%d tok_s=%.1f ttft_ms=%.1f",
                model_id, n, tps, (ttft or 0.0) * 1000,
            )
            if model_id:
                self._benchmarks[model_id] = result
                await loop.run_in_executor(None, self._remember_benchmark, result)
            return result

    def _remember_benchmark(self, result: dict) -> None:
        """Keep the result across a restart, under a key like the answer check's (same weights, backend,
        device, layer split, driver and library). Called with the inference lock held, so what is
        loaded cannot change underneath it. A fault here costs the persistence, never the benchmark."""
        try:
            key = self._key_now("benchmark", self.current_model_sha256())
            identity = self._loaded_file()
            if key is None or identity is None:
                return
            model_evidence.get_benchmark_store().put(key, {"model": identity, "result": result, "n_ctx": self._loaded_n_ctx})
            self._persisted_benchmarks = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("benchmark_not_saved error=%s", type(exc).__name__)


# Fixed prompt for the prompt-processing benchmark: about 700 tokens of
# ordinary English, so the measured rate reflects batch prompt evaluation
# rather than per-call overhead.
_PROMPT_RATE_TEXT = (
    "The quick brown fox jumps over the lazy dog near the old river bank while "
    "the evening light fades behind the hills and the town settles in. "
) * 40


_runtime: LLMRuntime | None = None


def get_runtime() -> LLMRuntime:
    global _runtime
    if _runtime is None:
        _runtime = LLMRuntime()
    return _runtime
