"""Model management router.

GET    /models - list available/installed models
GET    /models/{slug}/manifest - fetch a single model's manifest
POST   /models/{slug}/install - start download (background task)
DELETE /models/{slug} - delete installed model
POST   /models/{slug}/load - load model into runtime
POST   /models/{slug}/unload - unload model from runtime
GET    /models/{slug}/download-progress - poll download progress
POST   /models/{slug}/cancel-download - cancel an active download
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Literal

import psutil
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, ConfigDict, field_validator

from potluck_local.services import model_registry
from potluck_local.services.context_tuning import resolve_context_size
from potluck_local.services.inference_errors import InferenceFailure
from potluck_local.services.model_downloader import get_downloader
from potluck_local.services.model_lifecycle import get_model_lifecycle
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings
from potluck_local.utils.crypto import sha256_file
from potluck_local.services.model_identity import artifact_identity, valid_source_url
from potluck_local.utils.paths import models_dir

logger = logging.getLogger(__name__)
router = APIRouter()


def _safe_model_dir(slug: str) -> Path:
    """Resolve `slug` to a model directory strictly inside models_dir(), or
    raise 400. This is the only approved way to turn a caller-supplied slug
    into a filesystem path in this router.

    delete_model shutil.rmtrees whatever this returns, so a slug that
    escapes models_dir() is destructive, not just a bad-request: a slug of
    ".." (e.g. from a URL-encoded "%2e%2e" path segment, which FastAPI/
    Starlette decode to ".." before the handler ever sees it) resolves to
    models_dir()'s parent -- the whole ~/.potluck data directory -- and
    shutil.rmtree deletes it.

    Two independent checks, deliberately redundant:
      1. Reject slugs containing a path separator, "..", or a null byte
         outright -- catches the obvious cases without even touching the
         filesystem.
      2. Resolve the candidate path and assert its parent is exactly
         models_dir() (not merely "starts with" it, which a symlink or a
         cleverly-nested "a/../.." could defeat) -- catches anything the
         first check's blocklist didn't anticipate.
    """
    if (
        not slug
        or slug in (".", "..")
        or "/" in slug
        or "\\" in slug
        or "\x00" in slug
    ):
        raise HTTPException(status_code=400, detail=f"Invalid model slug '{slug}'.")

    base = models_dir().resolve()
    candidate = (base / slug).resolve()
    if candidate.parent != base:
        raise HTTPException(status_code=400, detail=f"Invalid model slug '{slug}'.")
    return candidate


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


@router.get("/setup-options")
async def get_setup_options() -> dict:
    from potluck_local.services.model_setup import setup_options
    return await asyncio.to_thread(setup_options)


@router.get("/available")
async def available_models() -> dict:
    from potluck_local.services.model_availability import model_availability
    return await model_availability()


class ModelTestRequest(BaseModel):
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    level: Literal["quick", "full"] = "quick"
    # True ignores a stored proof and runs the checks now.
    force: bool = False


@router.post("/{slug}/test")
async def test_local_model(slug: str, request: ModelTestRequest) -> dict:
    """Fixed, synthetic local-only prompts; no gateway, account or project content.

    Checks that the loaded model's answers are right (services/acceptance.py),
    reusing a stored proof unless `force`. With the check turned off in settings
    it only proves the model returns some text, and says the check was skipped.
    """
    from potluck_local.services import acceptance
    from potluck_local.services.model_identity import ModelIdentityError
    _safe_model_dir(slug)
    start = time.monotonic()
    runtime = get_runtime()
    verify = get_settings().runtime.verify_answers
    try:
        if verify:
            report, output_tokens = await runtime.accept_loaded_async(
                expected_model=slug, expected_sha256=request.sha256, level=request.level, force=request.force,
            )
        else:
            result = await runtime.chat(
                [{"role": "user", "content": "Reply briefly: what is 2 + 2? /no_think"}],
                expected_model=slug, expected_sha256=request.sha256, temperature=0, max_tokens=64,
            )
    except ModelIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except InferenceFailure as exc:
        raise HTTPException(503, exc.message) from exc
    except Exception as exc:
        raise HTTPException(503, "Local inference test failed. Check model and runtime settings.") from exc
    if verify:
        if report["status"] != acceptance.PASSED:
            raise HTTPException(503, report["message"] or acceptance.MODEL_FAILED_TEXT)
    else:
        content = (result.get("choices") or [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(503, "The local model returned no text. Check its chat template or context settings.")
        output_tokens = (result.get("usage") or {}).get("completion_tokens")
        report = acceptance.summary(status=acceptance.SKIPPED, model_id=slug)
    return {"schemaVersion": 1, "status": "passed", "model": slug,
            "durationMs": round((time.monotonic() - start) * 1000),
            "outputTokens": output_tokens, "scope": "local", "acceptance": report}


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


class LockedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not valid_source_url(value):
            raise ValueError("Locked downloads require a public Hugging Face GGUF URL without credentials or query parameters.")
        return value


class LockedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$")
    format: str = Field(pattern=r"^gguf$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sizeBytes: int = Field(strict=True, gt=0, le=9007199254740991)
    source: LockedSource | None = None


@router.get("/{slug}/identity")
async def installed_model_identity(slug: str) -> dict:
    path = _safe_model_dir(slug) / "model.gguf"
    try:
        digest, stamp = await asyncio.to_thread(artifact_identity, path)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Install this model before pinning it.") from exc
    except OSError as exc:
        raise HTTPException(409, "Model changed or could not be read during verification. Retry when installation finishes.") from exc
    model = {"id": slug, "format": "gguf", "sha256": digest, "sizeBytes": stamp[2]}
    manifest = model_registry.get_manifest(slug) or {}
    source = manifest.get("download_url", "")
    # An imported/older file can still be pinned offline, but never pair its
    # digest with provenance for different catalog bytes.
    if manifest.get("sha256") == digest and valid_source_url(source):
        model["source"] = {"url": source}
    return {"model_lock_version": 1, "model": model}


@router.post("/{slug}/install-locked", status_code=202)
async def install_locked_model(slug: str, model: LockedModel, background_tasks: BackgroundTasks) -> dict:
    if model.id != slug:
        raise HTTPException(400, "Model ID does not match the lock.")
    path = _safe_model_dir(slug) / "model.gguf"
    if path.exists():
        try:
            digest, stamp = await asyncio.to_thread(artifact_identity, path)
        except OSError as exc:
            raise HTTPException(409, "Existing model could not be verified; retry when installation finishes.") from exc
        if digest != model.sha256 or stamp[2] != model.sizeBytes:
            raise HTTPException(409, "A different file is already installed under this model ID. Move it aside deliberately before syncing this lock.")
        return {"model_lock_version": 1, "status": "verified", "sha256": digest}
    if model.source is None:
        raise HTTPException(400, "This lock has no download source. Copy its exact GGUF to the local model directory, then run sync again.")
    downloader = get_downloader()
    progress = downloader.get_progress(slug)
    if progress and progress.status in ("downloading", "verifying"):
        raise HTTPException(409, "A download is already running for this model. Wait for it, then run sync to verify the lock.")
    background_tasks.add_task(downloader.download_model, model_id=slug,
                             url=model.source.url, expected_sha256=model.sha256,
                             expected_size=model.sizeBytes)
    return {"model_lock_version": 1, "status": "downloading", "sha256": model.sha256}


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

    model_path = _safe_model_dir(slug) / "model.gguf"
    if model_path.exists():
        return {"status": "already_installed"}

    downloader = get_downloader()
    existing_progress = downloader.get_progress(slug)
    if existing_progress is not None and existing_progress.status in ("downloading", "verifying"):
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
    async with get_model_lifecycle().command():
        return await _delete_model(slug)


async def _delete_model(slug: str) -> dict:
    model_dir = _safe_model_dir(slug)
    if not model_dir.exists():
        raise HTTPException(status_code=404, detail=f"Model '{slug}' is not installed.")

    get_model_lifecycle().forget(slug)
    runtime = get_runtime()
    if runtime.is_loaded() and runtime.current_model_id() == slug:
        await runtime.unload_model_async()

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
    peers, no explicit addresses. The sidecar queries the roster, runs topology-aware
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
    Big Model Mode - used to run a model too big to fit on this machine
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


def _load_verify_reference(slug: str, manifest: dict | None, model_dir) -> tuple[str, str]:
    """Pick the sha256 to check a model against at load time, and where it came from.

    Threat: the opt-in load-time re-check exists to catch post-download tampering.
    But its reference hash must come from a source the tamperer can't also rewrite.
    The catalog (models.json) ships read-only inside the code-signed app bundle, so
    its sha256 is tamper-resistant -> prefer it. The model.gguf.sha256 written next
    to the weights lives in the user-writable ~/.potluck/models dir; anyone who can
    rewrite model.gguf can rewrite it too, so it only catches accidental corruption.
    Use it only as a fallback for a non-catalog model (which has no bundled hash).

    Returns (expected_hash_lowercased, source) where source is "catalog", "sidecar",
    or "none" when no reference hash is available.
    """
    if manifest is not None:
        h = str(manifest.get("sha256", "")).strip().lower()
        if h:
            return h, "catalog"
    sidecar = model_dir / "model.gguf.sha256"
    if sidecar.exists():
        try:
            h = sidecar.read_text().strip().lower()
        except OSError:
            h = ""
        if h:
            return h, "sidecar"
    return "", "none"


@router.post("/{slug}/load", status_code=200)
async def load_model(slug: str, body: LoadModelRequest | None = None) -> dict:
    lifecycle = get_model_lifecycle()
    async with lifecycle.command():
        # A distributed switch must not restore the previous local model or
        # reconnect peers on the next launch. HF/pipeline restore is separate.
        remote = body and (body.rpc_servers or body.pipeline or body.pipeline_auto)
        return await lifecycle.load_and_remember(_load_model(slug, body), None if remote else slug)


async def _load_model(slug: str, body: LoadModelRequest | None = None) -> dict:
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

            # Topology-aware short-circuit: the planner found the model fits on this
            # machine's VRAM alone, so it declined to build a pipeline (no hop is faster
            # than no hop, and no peer stages were started). Load the full model locally
            # via the whole-model engine instead of forcing a split. Off the event loop
            # (multi-second blocking load) like load_pipeline.
            if chain.get("mode") == "whole":
                logger.info("load_whole_model_start model_id=%s (fits on head; no split)", model_id)
                try:
                    result = await runtime.load_whole_model_async(
                        model_id, device=ac.device, dtype=chain.get("dtype"),
                        continuous_batch=body.continuous_batch,
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
            result = await runtime.load_pipeline_async(
                model_id, split=split, peer=peer, port=port,
                split2=split2, device=device, dtype=dtype,
                continuous_batch=body.continuous_batch,
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

    model_path = _safe_model_dir(slug) / "model.gguf"
    if not model_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Model '{slug}' is not installed. POST /models/{slug}/install first.",
        )

    # Fetch the catalog manifest once: used both by the opt-in integrity re-check
    # below and by the n_ctx resolution further down. None for a non-catalog model.
    manifest = model_registry.get_manifest(slug)

    # Opt-in integrity re-check before load. Off by default: a full re-hash of a
    # multi-GB file adds seconds to every load. Enable with
    # POTLUCK_VERIFY_MODEL_ON_LOAD=1 to catch post-download tampering or a
    # manually-dropped file. The reference hash comes from the catalog when the
    # model is one we ship (tamper-resistant; see _load_verify_reference), not from
    # the co-located sidecar an on-disk tamperer could rewrite.
    if os.environ.get("POTLUCK_VERIFY_MODEL_ON_LOAD") == "1":
        expected, reference = _load_verify_reference(slug, manifest, models_dir() / slug)
        if expected:
            loop = asyncio.get_running_loop()
            actual = await loop.run_in_executor(None, lambda: sha256_file(model_path))
            if actual != expected:
                logger.error("load_verify_mismatch slug=%s reference=%s", slug, reference)
                raise HTTPException(
                    status_code=500,
                    detail=f"Model '{slug}' failed its integrity check (sha256 mismatch); refusing to load.",
                )
            logger.info("load_verify_ok slug=%s reference=%s", slug, reference)
        else:
            logger.warning("load_verify_skipped slug=%s reason=no_reference_hash", slug)

    settings = get_settings()
    runtime = get_runtime()

    rpc_servers = body.rpc_servers if body and body.rpc_servers else None

    # Resolve n_ctx: auto-tune from the model + RAM when context_size is 0,
    # otherwise honor the user's pin (capped at the model's max). For a
    # non-catalog model we have no manifest, so fall back to the pin or a
    # safe default. (manifest was fetched above, before the integrity re-check.)
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
        result = await runtime.load_model_async(
            str(model_path),
            n_ctx=n_ctx,
            n_gpu_layers=settings.runtime.gpu_layers,
            n_threads=settings.runtime.threads or None,
            rpc_servers=rpc_servers,
            verify_answers=settings.runtime.verify_answers,
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
    lifecycle = get_model_lifecycle()
    async with lifecycle.command():
        lifecycle.forget(slug)
        runtime = get_runtime()
        if runtime.is_loaded() and runtime.current_model_id() == slug:
            await runtime.unload_model_async()
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
