"""Model management router.

GET    /models                              — list available/installed models
GET    /models/{slug}/manifest              — fetch a single model's manifest
POST   /models/{slug}/install               — start download (background task)
DELETE /models/{slug}                       — delete installed model
POST   /models/{slug}/load                  — load model into runtime
POST   /models/{slug}/unload               — unload model from runtime
GET    /models/{slug}/download-progress     — poll download progress
POST   /models/{slug}/cancel-download      — cancel an active download
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

import psutil
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from potluck_local.services import model_registry
from potluck_local.services.context_tuning import resolve_context_size
from potluck_local.services.model_downloader import get_downloader
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings
from potluck_local.utils.crypto import sha256_file
from potluck_local.utils.paths import models_dir

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def list_models() -> list[dict]:
    try:
        models = model_registry.list_models()
    except Exception:
        logger.exception("model_registry.list_models failed")
        return []
    # Merge in each model's last benchmark (tokens/sec + TTFT), if it has been run.
    benches = get_runtime().benchmarks()
    for m in models:
        b = benches.get(m.get("slug"))
        if b:
            m["benchmark"] = b
    return models


@router.post("/benchmark")
async def benchmark_loaded_model() -> dict:
    """Benchmark the currently loaded model: tokens/sec + time-to-first-token."""
    runtime = get_runtime()
    if not runtime.is_loaded():
        raise HTTPException(status_code=503, detail="No model loaded to benchmark.")
    try:
        return await runtime.benchmark()
    except Exception as exc:  # noqa: BLE001
        logger.exception("benchmark_failed")
        raise HTTPException(status_code=500, detail=f"benchmark failed: {type(exc).__name__}")


@router.get("/{slug}/manifest")
async def get_manifest(slug: str) -> dict:
    manifest = model_registry.get_manifest(slug)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No manifest found for '{slug}'.")
    return manifest


@router.post("/{slug}/install", status_code=202)
async def install_model(slug: str, background_tasks: BackgroundTasks) -> dict:
    manifest = model_registry.get_manifest(slug)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Model '{slug}' not found in registry.")

    # Integrity gate: a catalog entry must carry a sha256 so the download is
    # verified. The downloader skips verification on an empty hash (a utility
    # affordance for direct/test calls); refuse it here so a catalog regression
    # can never install an unverified model.
    if not str(manifest.get("sha256", "")).strip():
        raise HTTPException(
            status_code=500,
            detail=f"Catalog entry for '{slug}' has no sha256; refusing to install unverified.",
        )

    model_path = models_dir() / slug / "model.gguf"
    if model_path.exists():
        return {"status": "already_installed"}

    downloader = get_downloader()
    existing_progress = downloader.get_progress(slug)
    if existing_progress is not None and existing_progress.status == "downloading":
        return {"status": "already_downloading"}

    background_tasks.add_task(
        downloader.download_model,
        model_id=slug,
        url=manifest["download_url"],
        expected_sha256=manifest.get("sha256", ""),
        expected_size=manifest["size_bytes"],
    )

    logger.info("install_started slug=%s", slug)
    return {"status": "downloading", "slug": slug}


@router.delete("/{slug}", status_code=200)
async def delete_model(slug: str) -> dict:
    model_dir = models_dir() / slug
    if not model_dir.exists():
        raise HTTPException(status_code=404, detail=f"Model '{slug}' is not installed.")

    runtime = get_runtime()
    if runtime.is_loaded() and runtime.current_model_id() == slug:
        runtime.unload_model()

    shutil.rmtree(model_dir)
    logger.info("model_deleted slug=%s", slug)
    return {"status": "deleted"}


class PipelineConfig(BaseModel):
    """Pipeline-parallel strategy. This node loads the HEAD layer block and drives
    generation across one or more downstream peer stages (mid/tail) over the mesh: a
    model too big for any single peer, moving activations (not weights) across the wire.

    Distinct from rpc_servers (llama.cpp Big Model Mode, LAN-only weight-streaming):
    pipeline is transformers-based and WAN-viable. Scope: my-machines / trusted-circle
    only (activation privacy is unsolved -> never the open pool)."""

    split: int = Field(..., description="first layer boundary; head holds layers [0:split]")
    peer: str = Field(..., description="downstream next-hop mesh IP (the mid, or the tail in a 2-node chain)")
    port: int = Field(..., description="downstream next-hop listen port")
    split2: int = Field(default=0, description="second boundary for a 3-node chain (mid=[split:split2], tail=[split2:]); 0 => 2-node")
    device: str = Field(default="", description="cuda|mps|cpu|dml; autodetect if empty")
    dtype: str | None = Field(default=None, description="float16|bfloat16|float32; must match every node in the chain")


class PipelineAutoConfig(BaseModel):
    """Auto pipeline-parallel: orchestrate a chain across this node + available household
    peers, no explicit addresses. The sidecar queries the roster, runs TOPOLOGY-AWARE
    placement (layers proportional to each node's VRAM; nodes ordered by link latency;
    fewest hops that fit, capped at max_nodes), starts each peer's stage, then head-loads.
    If the model fits on this machine alone, the split is skipped (strategy='whole-model')
    and the response signals to load locally instead."""

    device: str = Field(default="", description="head device: cuda|mps|cpu|dml; autodetect if empty")
    dtype: str = Field(default="float16", description="uniform dtype across the chain (fp16 = safe on every backend incl DirectML)")
    max_nodes: int = Field(default=3, description="head + up to (max_nodes-1) peers; engine caps at 3")


class LoadModelRequest(BaseModel):
    """Optional body for POST /models/{slug}/load.

    rpc_servers: when set, the model loads with one or more remote
    llama.cpp RPC servers participating as ggml-backend devices. This is
    Big Model Mode — used to run a model too big to fit on this machine
    alone, by spreading weights/KV/compute across household peers that
    are running `rpc-server`. Endpoints look like "100.64.0.6:50052".
    Requires the Potluck-patched llama-cpp-python wheel.

    pipeline: when set, load in pipeline-parallel strategy instead (transformers head
    block + downstream peer stages). Mutually exclusive with rpc_servers. Uses
    hf_model_id (or the {slug} path param) as the HF model id, not a GGUF file.
    """

    rpc_servers: list[str] | None = Field(default=None)
    pipeline: PipelineConfig | None = Field(default=None)
    pipeline_auto: PipelineAutoConfig | None = Field(default=None)
    hf_model_id: str | None = Field(
        default=None,
        description="pipeline strategy: HF model id to load (e.g. 'Qwen/Qwen2.5-32B-Instruct'); defaults to the {slug} path param",
    )
    continuous_batch: bool = Field(
        default=False,
        description=(
            "Opt-in shared batching for the transformers paths (whole-model + pipeline): "
            "concurrent requests share batched forward passes instead of serialising. "
            "Default off -> single-stream path is byte-for-byte unchanged. Ignored on the "
            "llama.cpp/GGUF path (no transformers batcher there)."
        ),
    )


@router.post("/{slug}/load", status_code=200)
async def load_model(slug: str, body: LoadModelRequest | None = None) -> dict:
    # Pipeline-parallel: explicit chain (body.pipeline) or auto-orchestrated across the
    # household (body.pipeline_auto). Either path -> runtime.load_pipeline. Transformers
    # head-loop on an HF model id (no GGUF on disk), so it bypasses the slug/GGUF path.
    # Run the blocking, multi-second work off the event loop.
    if body and (body.pipeline or body.pipeline_auto):
        runtime = get_runtime()
        model_id = body.hf_model_id or slug
        loop = asyncio.get_running_loop()
        chain_nodes = None
        stage_peers = []  # peers whose stage we started; torn down if the head load fails

        if body.pipeline_auto:
            ac = body.pipeline_auto
            from potluck_local.services.pipeline_parallel import orchestrate_household_pipeline
            try:
                chain = await loop.run_in_executor(
                    None,
                    lambda: orchestrate_household_pipeline(
                        model_id, device=ac.device, dtype=ac.dtype, max_nodes=ac.max_nodes,
                    ),
                )
            except Exception as exc:
                logger.error("pipeline_orchestrate_error model_id=%s error=%s", model_id, exc)
                raise HTTPException(status_code=502, detail=f"pipeline orchestration failed: {exc}")

            # Topology-aware short-circuit: the planner found the model fits on THIS
            # machine's VRAM alone, so it declined to build a pipeline (no hop is faster
            # than no hop, and no peer stages were started). Load the FULL model locally
            # via the whole-model engine instead of forcing a split. Off the event loop
            # (multi-second blocking load) like load_pipeline.
            if chain.get("mode") == "whole":
                logger.info("load_whole_model_start model_id=%s (fits on head; no split)", model_id)
                try:
                    result = await loop.run_in_executor(
                        None,
                        lambda: runtime.load_whole_model(
                            model_id, device=ac.device, dtype=chain.get("dtype"),
                            continuous_batch=body.continuous_batch,
                        ),
                    )
                except Exception as exc:
                    logger.error("load_whole_model_error model_id=%s error=%s", model_id, exc)
                    raise HTTPException(status_code=500, detail=f"whole-model load failed: {exc}")
                result["split_skipped"] = True
                result["loaded"] = True
                result["detail"] = (
                    "Model fits on this machine's VRAM alone; the pipeline split was "
                    "skipped and the full model was loaded locally."
                )
                logger.info(
                    "load_whole_model_complete model_id=%s load_time=%.2f",
                    model_id, result.get("load_time_seconds", 0),
                )
                return result

            split, split2, peer, port = chain["split"], chain["split2"], chain["peer"], chain["port"]
            device, dtype = ac.device, chain["dtype"]
            chain_nodes = chain.get("nodes")
            stage_peers = chain.get("stage_peers", [])
        else:
            pc = body.pipeline
            split, split2, peer, port = pc.split, pc.split2, pc.peer, pc.port
            device, dtype = pc.device, pc.dtype

        logger.info(
            "load_pipeline_start model_id=%s split=%s split2=%s downstream=%s:%s",
            model_id, split, split2, peer, port,
        )
        try:
            result = await loop.run_in_executor(
                None,
                lambda: runtime.load_pipeline(
                    model_id, split=split, peer=peer, port=port,
                    split2=split2, device=device, dtype=dtype,
                    continuous_batch=body.continuous_batch,
                ),
            )
        except Exception as exc:
            logger.error("load_pipeline_error model_id=%s error=%s", model_id, exc)
            # Head load failed after peer stages were started -> tear them down so the
            # peers aren't left wedged holding their devices.
            if stage_peers:
                from potluck_local.services.pipeline_parallel import _stop_peer_stage
                for ip in reversed(stage_peers):
                    await loop.run_in_executor(None, lambda ip=ip: _stop_peer_stage(ip))
            raise HTTPException(status_code=500, detail=f"pipeline load failed: {exc}")
        if chain_nodes is not None:
            result["chain"] = chain_nodes
        logger.info("load_pipeline_complete model_id=%s load_time=%.2f", model_id, result.get("load_time_seconds", 0))
        return result

    model_path = models_dir() / slug / "model.gguf"
    if not model_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Model '{slug}' is not installed. POST /models/{slug}/install first.",
        )

    # Opt-in integrity re-check against the sha256 recorded at download time.
    # Off by default: a full re-hash of a multi-GB model adds seconds to load.
    # Enable with POTLUCK_VERIFY_MODEL_ON_LOAD=1 to catch post-download tampering
    # or a manually-dropped file.
    if os.environ.get("POTLUCK_VERIFY_MODEL_ON_LOAD") == "1":
        sidecar = models_dir() / slug / "model.gguf.sha256"
        if sidecar.exists():
            expected = sidecar.read_text().strip().lower()
            loop = asyncio.get_running_loop()
            actual = await loop.run_in_executor(None, lambda: sha256_file(model_path))
            if expected and actual != expected:
                logger.error("load_verify_mismatch slug=%s", slug)
                raise HTTPException(
                    status_code=500,
                    detail=f"Model '{slug}' failed its integrity check (sha256 mismatch); refusing to load.",
                )
            logger.info("load_verify_ok slug=%s", slug)

    settings = get_settings()
    runtime = get_runtime()

    rpc_servers = body.rpc_servers if body and body.rpc_servers else None

    # Resolve n_ctx: auto-tune from the model + RAM when context_size is 0,
    # otherwise honor the user's pin (capped at the model's max). For a
    # non-catalog model we have no manifest, so fall back to the pin or a
    # safe default.
    manifest = model_registry.get_manifest(slug)
    ram_total_gb = psutil.virtual_memory().total / (1024 ** 3)
    if manifest is not None:
        n_ctx = resolve_context_size(
            configured=settings.runtime.context_size,
            model_max_ctx=int(manifest.get("context_window", 0) or 0),
            size_bytes=int(manifest.get("size_bytes", 0) or 0),
            num_layers=int(manifest.get("num_layers", 0) or 0),
            ram_total_gb=ram_total_gb,
        )
    else:
        n_ctx = settings.runtime.context_size or 4096

    logger.info(
        "load_model_start slug=%s n_ctx=%d (configured=%d, ram_gb=%.1f) rpc_servers=%s",
        slug,
        n_ctx,
        settings.runtime.context_size,
        ram_total_gb,
        ",".join(rpc_servers) if rpc_servers else "(none)",
    )
    try:
        result = runtime.load_model(
            str(model_path),
            n_ctx=n_ctx,
            n_gpu_layers=settings.runtime.gpu_layers,
            n_threads=settings.runtime.threads or None,
            rpc_servers=rpc_servers,
        )
    except TypeError as exc:
        # Stock (non-Potluck-patched) llama-cpp-python doesn't accept the
        # rpc_servers kwarg. Surface a clean error so the frontend can
        # tell the user to upgrade their wheel.
        if rpc_servers and "rpc_servers" in str(exc):
            raise HTTPException(
                status_code=501,
                detail=(
                    "Big Model Mode requires the Potluck-patched "
                    "llama-cpp-python wheel. This sidecar appears to be running "
                    "stock llama-cpp-python. Install the patched wheel from "
                    "~/Documents/potluck-parallel/llama-cpp-python or rebuild "
                    "with CMAKE_ARGS='-DGGML_RPC=ON'."
                ),
            )
        raise
    logger.info("load_model_complete slug=%s load_time=%.2f", slug, result.get("load_time_seconds", 0))
    return result


@router.post("/{slug}/unload", status_code=200)
async def unload_model(slug: str) -> dict:
    runtime = get_runtime()
    if runtime.is_loaded() and runtime.current_model_id() == slug:
        runtime.unload_model()
        logger.info("model_unloaded slug=%s", slug)
    return {"status": "unloaded"}


@router.get("/{slug}/download-progress")
async def download_progress(slug: str) -> dict:
    downloader = get_downloader()
    progress = downloader.get_progress(slug)
    if progress is None:
        raise HTTPException(status_code=404, detail=f"No download record for '{slug}'.")
    # DownloadProgress is a dataclass; return as dict for serialisation.
    if hasattr(progress, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(progress)
    return dict(progress)


@router.post("/{slug}/cancel-download", status_code=200)
async def cancel_download(slug: str) -> dict:
    downloader = get_downloader()
    downloader.cancel_download(slug)
    logger.info("download_cancelled slug=%s", slug)
    return {"status": "cancelled"}
