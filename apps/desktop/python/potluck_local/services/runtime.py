"""LLM inference runtime — wraps llama-cpp-python.

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
import threading
import time
from contextlib import aclosing, asynccontextmanager
from functools import partial
from pathlib import Path

from potluck_local.services.model_identity import ModelIdentityError, artifact_identity, file_stamp

logger = logging.getLogger(__name__)


class LLMRuntime:
    """Manages one loaded llama.cpp or transformers inference backend."""

    def __init__(self) -> None:
        self._model = None
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
        self._inference_lock = asyncio.Lock()
        # A batch of streams collectively owns _inference_lock. The admission
        # gate lets lifecycle operations stop new admissions before draining
        # the current batch, so an ongoing stream of requests cannot starve a swap.
        self._admission_lock = asyncio.Lock()
        self._batched_streams = 0
        # Last benchmark result per model_id (tokens/sec + TTFT). Persists across
        # loads so the Models page + peer rows can show "last bench" without
        # re-running. Populated by benchmark().
        self._benchmarks: dict[str, dict] = {}
        # Ensure unload runs before ggml-metal's atexit finalize on process exit.
        self._atexit_registered = False

    def is_loaded(self) -> bool:
        return self._model is not None or self._pipeline is not None

    def current_model_id(self) -> str | None:
        return self._model_id

    def current_model_sha256(self) -> str | None:
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
        """Last benchmark result keyed by model_id (tokens/sec + TTFT)."""
        return dict(self._benchmarks)

    def load_model(
        self,
        model_path: str,
        *,
        n_ctx: int = 4096,
        n_gpu_layers: int = -1,
        n_threads: int | None = None,
        rpc_servers: list[str] | None = None,
    ) -> dict:
        """Load a GGUF model. Blocks until loaded. Thread-safe swap via _load_lock.

        rpc_servers: optional list of llama.cpp RPC server endpoints (e.g.
        ["100.64.0.6:50052"]) to register as remote ggml-backend devices.
        When set, llama.cpp distributes weights + KV cache + compute across
        the local device(s) + RPC servers in proportion to each device's
        free memory. This is "Big Model Mode" — used when a model is too
        large to fit on any single household machine alone.
        """
        from llama_cpp import Llama  # lazy import — avoids Metal init at import time

        with self._load_lock:
            if self._model is not None or self._pipeline is not None:
                self._release_model()

            start = time.monotonic()
            effective_threads = n_threads or 0
            kwargs: dict = {
                "model_path": model_path,
                "n_ctx": n_ctx,
                "n_gpu_layers": n_gpu_layers,
                "n_threads": effective_threads,
                "verbose": False,
            }
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
            model = Llama(**kwargs)
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

            self._model = model
            self._model_path = model_path
            self._artifact = identity
            self._verified_artifact = verified_identity
            self._model_id = Path(model_path).parent.name
            self._loaded_n_ctx = n_ctx
            self._loaded_rpc_servers = tuple(rpc_servers) if rpc_servers else None
            # Register after Llama() so our atexit runs before ggml-metal's
            # (atexit is LIFO). Prevents the macOS "python quit unexpectedly /
            # Reopen" dialog when Metal finalize races process exit.
            if not self._atexit_registered:
                atexit.register(self.unload_model)
                self._atexit_registered = True

            logger.info(
                "model_loaded model_id=%s n_ctx=%d n_gpu_layers=%d n_threads=%s "
                "rpc_servers=%s load_time=%.2fs",
                self._model_id,
                n_ctx,
                n_gpu_layers,
                effective_threads or "auto",
                ",".join(rpc_servers) if rpc_servers else "(none)",
                elapsed,
            )

            return {
                "model_path": model_path,
                "model_id": self._model_id,
                "load_time_seconds": elapsed,
                "n_ctx": n_ctx,
                "n_gpu_layers": n_gpu_layers,
                "n_threads": effective_threads or None,
                "rpc_servers": list(rpc_servers) if rpc_servers else None,
            }

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
    ) -> dict:
        return await self._run_lifecycle(
            partial(self.load_model, model_path, n_ctx=n_ctx,
                    n_gpu_layers=n_gpu_layers, n_threads=n_threads,
                    rpc_servers=rpc_servers)
        )

    async def load_pipeline_async(self, model_id: str, **kwargs) -> dict:
        return await self._run_lifecycle(partial(self.load_pipeline, model_id, **kwargs))

    async def load_whole_model_async(self, model_id: str, **kwargs) -> dict:
        return await self._run_lifecycle(partial(self.load_whole_model, model_id, **kwargs))

    async def unload_model_async(self) -> None:
        await self._run_lifecycle(self.unload_model)

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
    ):
        """Yield OpenAI SSE with shared admission only for continuous batchers.

        Select the backend after admission, so a request queued behind a model
        swap uses the newly loaded backend. Sampling stays per request; the
        llama.cpp path retains its existing temperature/stop defaults.
        """
        async with self._stream_session() as batched:
            self.assert_model_identity(expected_model, expected_sha256)
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
                label = f"stream model_id={self._model_id}"

                def produce():
                    for chunk in model.create_chat_completion(
                        messages=messages, temperature=temperature, max_tokens=max_tokens,
                        stop=stop or [], stream=True,
                    ):
                        yield chunk["choices"][0].get("delta", {}).get("content")
            else:
                yield 'data: {"error":"No model loaded"}\n\n'
                yield "data: [DONE]\n\n"
                return

            # Explicitly close the nested generator before releasing model access
            # on disconnect. An async-for alone leaves cleanup to finalization.
            async with aclosing(self._stream_sse_from_blocking(
                produce, label=label, drain_on_cancel=batched,
            )) as stream:
                async for chunk in stream:
                    yield chunk

    async def _stream_sse_from_blocking(self, produce, *, label, drain_on_cancel=False):
        """Pump a blocking iterator into SSE; caller must hold _stream_session.

        Batchers own their decode loop and currently have no per-row cancel API.
        On disconnect, discard that row's output and drain to its terminal event
        before releasing model access. Other rows can continue independently.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel_event = threading.Event()

        def _generate() -> None:
            try:
                for piece in produce():
                    if cancel_event.is_set():
                        if drain_on_cancel:
                            continue
                        break
                    if piece:  # skip empty / None deltas
                        loop.call_soon_threadsafe(queue.put_nowait, piece)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as exc:
                logger.error("%s error=%s", label, type(exc).__name__)
                loop.call_soon_threadsafe(queue.put_nowait, f"\x00ERROR\x00{exc}")
                loop.call_soon_threadsafe(queue.put_nowait, None)

        fut = loop.run_in_executor(None, _generate)
        token_count = 0
        start = time.monotonic()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, str) and item.startswith("\x00ERROR\x00"):
                    err_msg = item[len("\x00ERROR\x00"):]
                    yield f'data: {json.dumps({"error": str(err_msg)})}\n\n'
                    break
                token_count += 1
                yield f'data: {json.dumps({"choices": [{"delta": {"content": item}}]})}\n\n'
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.info("%s cancelled tokens_so_far=%d", label, token_count)
        finally:
            cancel_event.set()
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

            fut = loop.run_in_executor(
                None,
                lambda: model.create_chat_completion(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                ),
            )
            try:
                result = await fut
            finally:
                # A cancelled request must not free the inference lock while the
                # worker thread is still inside create_chat_completion (the model
                # is not thread-safe). Wait for the thread to finish first. This
                # is a blocking call with no early-stop hook, so we drain fully.
                if not fut.done():
                    try:
                        await asyncio.shield(fut)
                    except Exception:
                        pass

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
                try:
                    for chunk in model.create_chat_completion(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stop=stop or [],
                        stream=True,
                    ):
                        if cancel_event.is_set():
                            break
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except Exception as exc:
                    logger.error("gateway_stream_error model_id=%s error=%s", model_id, type(exc).__name__)
                    loop.call_soon_threadsafe(queue.put_nowait, {"__error__": str(exc)})
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            fut = loop.run_in_executor(None, _generate)

            chunk_count = 0
            start = time.monotonic()
            try:
                while True:
                    item = await queue.get()
                    if item is None:
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
                try:
                    await asyncio.shield(fut)
                except Exception:
                    pass
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
            return result


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
