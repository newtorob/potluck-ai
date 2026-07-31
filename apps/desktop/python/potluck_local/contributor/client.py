"""Contributor sidecar — WebSocket client + lifecycle.

Connects to the coordinator's /peers/connect endpoint, completes the
Ed25519 attestation handshake, then sends periodic heartbeats. Reconnects
on any failure with exponential backoff.

Run as `python -m potluck_local.contributor`. Settings come from
POTLUCK_* environment variables (see config.py).

Architectural invariant (see manifesto §10.3): this process must never
write prompt content or member-identifying data to disk, logs, or
telemetry. The handshake + heartbeat loop here only handles routing
metadata. When job dispatch lands in m3-m4, the same invariant applies
to the inference path.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import platform as _platform
import socket
import sys
import uuid
from datetime import datetime, timezone

import time

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from potluck_local.contributor.config import Settings, get_settings
from potluck_local.contributor.keys import ensure_keypair, public_key_b64
from potluck_local.contributor.schemas import (
    AcceptMessage,
    AttestationPayload,
    CapabilityEntry,
    ChallengeMessage,
    HeartbeatMessage,
    JobAssignedMessage,
    JobCompleteMessage,
    JobErrorMessage,
    JobTokenMessage,
    RejectMessage,
)

logger = logging.getLogger("potluck.contributor")

# Module version — embedded in attestation, persisted by coordinator into
# peer_heartbeats.client_version. Bump when the wire protocol or
# heartbeat shape changes.
CLIENT_VERSION = "0.0.1-dev"

# How long to wait for an accept/reject after sending attestation.
HANDSHAKE_RESPONSE_TIMEOUT_S = 10.0

# Reconnect backoff bounds.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0


class HandshakeFailed(Exception):
    """Wraps a coordinator reject — caller treats this as fatal vs.
    transient based on context."""


def detect_platform() -> str:
    """Cross-platform identifier, e.g. 'darwin-arm64', 'linux-x86_64'.

    Note Windows reports machine() as 'AMD64' rather than 'x86_64' — we
    keep what platform.machine() returns rather than normalizing, so the
    string is faithful to what the underlying OS calls itself. Coordinator
    treats this as informational, not a routing key.
    """
    return f"{_platform.system().lower()}-{_platform.machine().lower()}"


def detect_hardware_summary() -> dict[str, object]:
    """Minimal hardware metadata. Real GPU detection lands in m3+ when the
    coordinator actually routes by capability. For m1 we capture enough
    to make the /transparency/fleet view useful (cpu count, OS release)."""
    summary: dict[str, object] = {
        "system": _platform.system(),
        "release": _platform.release(),
        "machine": _platform.machine(),
        "cpu_count": _platform.os.cpu_count() if hasattr(_platform, "os") else None,
    }
    return summary


def _quantization_from_slug(slug: str) -> str:
    """Best-effort quantization extraction from a model slug.

    Recognises common patterns:
      - "qwen2.5:3b-instruct-q4_K_M" -> "q4_K_M"
      - "qwen-2.5-3b-instruct-q4"    -> "q4"
      - "llama-3.1-8b-instruct-q5_k_m" -> "q5_k_m"
    Returns "unknown" if no quant tag is recognisable.
    """
    import re
    m = re.search(r"\b(q\d+(?:_[a-zA-Z0-9]+)*|f16|f32|bf16)\b", slug, re.IGNORECASE)
    return m.group(1) if m else "unknown"


async def _build_capabilities(settings: Settings) -> list[CapabilityEntry]:
    """Enumerate the models this peer is willing to serve, for the
    coordinator's peer_capabilities table.

    Per backend:
      - inference_enabled=False: empty list (peer is stub-echo only).
      - llama-cpp: a single entry derived from settings.model_slug.
      - Ollama: query /api/tags and emit one entry per installed model.
        That way a peer that has both qwen2.5:3b AND llama3.1:8b pulled
        can serve jobs targeting either, with no manual config needed.
    """
    if not settings.inference_enabled:
        return []

    if settings.inference_backend == "ollama":
        # Hit /api/tags to discover what models Ollama actually has.
        # If Ollama is down at handshake time, we surface an empty list
        # rather than failing the handshake - the peer can still join
        # the network as stub-echo; capabilities refresh on reconnect.
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
                r.raise_for_status()
                models = r.json().get("models", [])
            entries: list[CapabilityEntry] = []
            for m in models:
                name = m.get("name", "")
                if not name:
                    continue
                entries.append(CapabilityEntry(
                    model_slug=name,
                    quantization=_quantization_from_slug(name),
                ))
            return entries
        except Exception as exc:
            logger.warning(
                "ollama_capabilities_probe_failed reason=%s; advertising no capabilities",
                exc,
            )
            return []

    # llama-cpp backend (default)
    if settings.model_slug:
        return [CapabilityEntry(
            model_slug=settings.model_slug,
            quantization=_quantization_from_slug(settings.model_slug),
        )]
    return []


async def _do_handshake(
    ws: websockets.WebSocketClientProtocol,
    settings: Settings,
    priv: Ed25519PrivateKey,
) -> AcceptMessage:
    # 1) receive challenge
    raw = await ws.recv()
    challenge = ChallengeMessage.model_validate_json(raw)
    nonce_bytes = base64.urlsafe_b64decode(
        challenge.nonce + "=" * (-len(challenge.nonce) % 4)
    )

    # 2) sign + send attest
    sig = priv.sign(nonce_bytes)
    capabilities = await _build_capabilities(settings)
    attest = AttestationPayload(
        member_id=settings.member_id,
        peer_public_key=public_key_b64(priv.public_key()),
        binary_hash=settings.binary_hash,
        client_version=CLIENT_VERSION,
        platform=detect_platform(),
        hostname=socket.gethostname(),
        hardware_summary=detect_hardware_summary(),
        nonce_signature=base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
        capabilities=capabilities,
    )
    if capabilities:
        logger.info(
            "advertising_capabilities backend=%s slugs=%s",
            settings.inference_backend,
            [c.model_slug for c in capabilities],
        )
    await ws.send(attest.model_dump_json())

    # 3) receive accept or reject
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_RESPONSE_TIMEOUT_S)
    msg = json.loads(raw)
    msg_type = msg.get("type")
    if msg_type == "accept":
        return AcceptMessage.model_validate(msg)
    if msg_type == "reject":
        reject = RejectMessage.model_validate(msg)
        raise HandshakeFailed(reject.reason)
    raise HandshakeFailed(f"unexpected post-attestation message: {msg_type}")


async def _heartbeat_loop(
    ws: websockets.WebSocketClientProtocol,
    accept: AcceptMessage,
) -> None:
    """Send a heartbeat every `heartbeat_interval_s`. Returns when the
    connection closes; caller handles reconnect."""
    while True:
        await asyncio.sleep(accept.heartbeat_interval_s)
        hb = HeartbeatMessage(
            peer_id=accept.peer_id,
            session_id=accept.session_id,
            timestamp=datetime.now(timezone.utc),
            current_jobs=0,
            gpu_utilization=None,
            vram_available_gb=None,
        )
        await ws.send(hb.model_dump_json())
        logger.debug("heartbeat_sent peer_id=%s", accept.peer_id)


# m3 stub: per-token "inference" delay so the streaming feels real on the
# coordinator + desktop side. Replaced by llama-cpp-python's actual
# generation rate when real inference lands.
_STUB_TOKEN_DELAY_S = 0.03


async def _handle_real_job(
    ws: websockets.WebSocketClientProtocol,
    job: JobAssignedMessage,
    settings: Settings,
) -> None:
    """Real-inference handler (Phase G m3 step 5).

    Lazy-loads the configured GGUF on first job, streams generated tokens
    back as JobTokenMessages, sends JobCompleteMessage when done. If the
    job requests a model_slug the peer doesn't serve, replies with
    JobErrorMessage(error_category="declined") so the coordinator can
    reroute to a peer that does.

    Privacy invariant: prompt content + generated tokens never leave
    process memory beyond the WS frames they're emitted in. No log
    statements include token text or prompt content; counters only.
    """
    started = time.monotonic()

    # Decide whether this peer can serve the requested model, per backend, and
    # which slug to actually load. This must agree with _build_capabilities:
    #   - ollama: the peer advertised EVERY installed Ollama model, so serve
    #     whatever the coordinator routed (job.model_slug). Ollama loads it on
    #     demand; the engine's _ensure_loaded reports if it isn't installed.
    #   - llama-cpp: the peer has exactly one GGUF (settings.model_slug) and can
    #     only serve that slug.
    # Using settings.model_slug for the dispatch check (the old behaviour) made a
    # multi-model Ollama peer decline every model except its one configured slug.
    if settings.inference_backend == "ollama":
        serve_slug = job.model_slug
    elif settings.model_slug != job.model_slug:
        err = JobErrorMessage(
            job_id=job.job_id,
            error_category="declined",
            error_message=(
                f"this peer serves '{settings.model_slug}', not '{job.model_slug}'"
            ),
        )
        await ws.send(err.model_dump_json())
        logger.info(
            "real_job_declined job_id=%s reason=model_mismatch served=%s requested=%s",
            job.job_id, settings.model_slug, job.model_slug,
        )
        return
    else:
        serve_slug = settings.model_slug

    # Lazy import so peers without inference enabled don't pay the
    # import cost at startup (and so a missing llama_cpp install doesn't
    # break the stub-echo path on smaller machines).
    from potluck_local.contributor.inference import (
        get_llamacpp_engine,
        get_ollama_engine,
    )

    try:
        if settings.inference_backend == "ollama":
            engine = get_ollama_engine(
                base_url=settings.ollama_url,
                model_slug=serve_slug,
                n_ctx=settings.n_ctx,
            )
        else:
            if settings.model_path is None:
                err = JobErrorMessage(
                    job_id=job.job_id,
                    error_category="model_not_loaded",
                    error_message="model_path not configured for llama-cpp backend",
                )
                await ws.send(err.model_dump_json())
                return
            engine = get_llamacpp_engine(
                model_path=settings.model_path,
                model_slug=serve_slug,
                n_ctx=settings.n_ctx,
                n_gpu_layers=settings.n_gpu_layers,
            )
    except Exception as exc:
        logger.exception("real_job_engine_init_failed job_id=%s", job.job_id)
        err = JobErrorMessage(
            job_id=job.job_id,
            error_category="internal",
            error_message=str(exc)[:200],
        )
        await ws.send(err.model_dump_json())
        return

    token_index = 0
    # Choose the inference path based on what the coordinator sent:
    #   job.messages populated -> stream_chat (applies the model's chat template,
    #     multi-turn context).  Used when scope==circle or pool_multiturn is on.
    #   job.messages absent/empty -> stream_completion (single-turn, prompt string).
    #     Backward-compat default.
    sampling_kwargs: dict = dict(
        max_tokens=job.sampling.max_tokens,
        temperature=job.sampling.temperature,
        top_p=job.sampling.top_p,
        top_k=job.sampling.top_k,
        repeat_penalty=job.sampling.repeat_penalty,
        stop_sequences=job.sampling.stop_sequences,
        seed=job.sampling.seed,
    )
    try:
        if job.messages:
            messages_as_dicts = [{"role": m.role, "content": m.content} for m in job.messages]
            token_source = engine.stream_chat(messages=messages_as_dicts, **sampling_kwargs)
        else:
            token_source = engine.stream_completion(prompt=job.prompt, **sampling_kwargs)
        async for chunk in token_source:
            tok = JobTokenMessage(
                job_id=job.job_id,
                token_index=token_index,
                token_text=chunk,
            )
            await ws.send(tok.model_dump_json())
            token_index += 1

        duration_ms = int((time.monotonic() - started) * 1000)
        # Heuristic: if we hit max_tokens we report "length"; otherwise the
        # model decided to stop. llama-cpp-python's chunks carry a
        # finish_reason but we don't surface it through the queue today;
        # adding it is a minor follow-up.
        finish_reason = "length" if token_index >= job.sampling.max_tokens else "stop"
        cmp = JobCompleteMessage(
            job_id=job.job_id,
            output_tokens=token_index,
            duration_ms=duration_ms,
            finish_reason=finish_reason,
        )
        await ws.send(cmp.model_dump_json())
        logger.info(
            "real_job_complete job_id=%s output_tokens=%d duration_ms=%d reason=%s",
            job.job_id, token_index, duration_ms, finish_reason,
        )
    except websockets.ConnectionClosed:
        logger.warning("real_job_aborted_connection_closed job_id=%s", job.job_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("real_job_internal_error job_id=%s", job.job_id)
        try:
            err = JobErrorMessage(
                job_id=job.job_id,
                error_category="internal",
                error_message=str(exc)[:200],
            )
            await ws.send(err.model_dump_json())
        except Exception:
            pass


async def _handle_stub_job(
    ws: websockets.WebSocketClientProtocol,
    job: JobAssignedMessage,
) -> None:
    """m3 stub inference handler — echoes the prompt back as tokens.

    Privacy invariant: this function MUST NOT log job.prompt content,
    write it to disk, or persist it anywhere. The whole point of the
    in-memory-only rule is that the prompt lives in RAM only as long as
    the inference takes, then evaporates. CI lint will eventually enforce
    this; for now, code review.
    """
    started = time.monotonic()
    # Stub "model" output: prefix the prompt + emit it character by
    # character. Real inference (llama-cpp-python) replaces this body in
    # step 5 of the m3 plan; the wire-protocol surrounding it doesn't
    # change, which is the whole point of doing the stub first.
    output = f"echo: {job.prompt}"
    max_chars = min(len(output), job.sampling.max_tokens)

    try:
        for idx in range(max_chars):
            tok = JobTokenMessage(
                job_id=job.job_id,
                token_index=idx,
                token_text=output[idx],
            )
            await ws.send(tok.model_dump_json())
            await asyncio.sleep(_STUB_TOKEN_DELAY_S)

        finish_reason = "length" if max_chars < len(output) else "stop"
        duration_ms = int((time.monotonic() - started) * 1000)
        cmp = JobCompleteMessage(
            job_id=job.job_id,
            output_tokens=max_chars,
            duration_ms=duration_ms,
            finish_reason=finish_reason,
        )
        await ws.send(cmp.model_dump_json())
        # Counter-only logging: count of tokens, never the content.
        logger.info(
            "stub_job_complete job_id=%s output_tokens=%d duration_ms=%d reason=%s",
            job.job_id, max_chars, duration_ms, finish_reason,
        )
    except websockets.ConnectionClosed:
        # Connection died mid-stream — coordinator will time out the job
        # on its side. Nothing useful to do here.
        logger.warning("stub_job_aborted_connection_closed job_id=%s", job.job_id)
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("stub_job_internal_error job_id=%s", job.job_id)
        try:
            err = JobErrorMessage(
                job_id=job.job_id,
                error_category="internal",
                error_message=str(exc)[:200],
            )
            await ws.send(err.model_dump_json())
        except Exception:
            pass


async def _receive_loop(
    ws: websockets.WebSocketClientProtocol,
    settings: Settings,
) -> None:
    """Read incoming server-sent messages and dispatch by type.

    Job handlers are spawned as separate tasks so token streaming for one
    job doesn't block reading the next message (e.g. a future second job,
    or a server-initiated cancellation). For m3 we only handle one job at
    a time per peer in practice — the coordinator's pick_peer is round-
    robin-ish and won't stack jobs — but the structure is here for when
    concurrent jobs land in m4.

    Every spawned handler task is tracked in `background_tasks` and, in the
    `finally` below, cancelled + awaited before this function returns. This
    closes a launch-blocking leak: WITHOUT tracking, a WS disconnect (or any
    other reason ws.recv() raises out of the `while True`) left an in-flight
    _handle_real_job task running detached in the background, still driving
    LlamaCppInferenceEngine's producer thread against a socket that's gone.
    Tracking + cancelling here is what lets that engine's own cancellation
    (stop_event + drain, see inference.py) actually run.
    """
    background_tasks: set[asyncio.Task] = set()
    try:
        while True:
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("server_message_not_json")
                continue
            msg_type = msg.get("type") if isinstance(msg, dict) else None

            if msg_type == "job_assigned":
                try:
                    job = JobAssignedMessage.model_validate(msg)
                except Exception as exc:
                    logger.warning("job_assigned_invalid_shape error=%s", exc)
                    continue
                # Privacy: log only message count + total chars, never content.
                if job.messages:
                    total_chars = sum(len(m.content) for m in job.messages)
                    logger.info(
                        "job_assigned_received job_id=%s model_slug=%s "
                        "message_count=%d total_chars=%d",
                        job.job_id, job.model_slug, len(job.messages), total_chars,
                    )
                else:
                    logger.info(
                        "job_assigned_received job_id=%s model_slug=%s prompt_chars=%d",
                        job.job_id, job.model_slug, len(job.prompt),
                    )
                if settings.inference_enabled:
                    task = asyncio.create_task(_handle_real_job(ws, job, settings))
                else:
                    task = asyncio.create_task(_handle_stub_job(ws, job))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
            else:
                logger.debug("server_message_unhandled type=%s", msg_type)
    finally:
        # The WS is closing/closed (or this loop is being torn down for any
        # other reason) — cancel and await every still-running job handler
        # rather than leaving it stranded. Copy to a list first: cancel()
        # doesn't mutate the set, but the done-callback (background_tasks
        # .discard) fires as each task actually finishes, which would mutate
        # the set while we're iterating it.
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


async def _session(settings: Settings, priv: Ed25519PrivateKey) -> None:
    logger.info("connecting url=%s", settings.coordinator_url)
    # Present the coordinator token on the WS upgrade when configured. The
    # coordinator's /peers/connect gate is a no-op unless POTLUCK_COORDINATOR_TOKEN
    # is set, so leaving this unset keeps tokenless household setups working.
    extra_headers = (
        [("X-Potluck-Coordinator-Token", settings.coordinator_token)]
        if settings.coordinator_token
        else None
    )
    async with websockets.connect(
        settings.coordinator_url, extra_headers=extra_headers
    ) as ws:
        try:
            accept = await _do_handshake(ws, settings, priv)
        except HandshakeFailed as e:
            logger.error("handshake_rejected reason=%s", e)
            # Reject is "fatal for this attempt" — most likely an unknown
            # binary_hash, bad sig, or member_id problem. Reconnecting
            # without changing config will just fail the same way; surface
            # the error and let the backoff slow us down.
            raise
        logger.info(
            "session_opened peer_id=%s session_id=%s heartbeat_interval=%ds",
            accept.peer_id,
            accept.session_id,
            accept.heartbeat_interval_s,
        )
        # Bidirectional: heartbeat outbound, receive incoming. First task
        # to terminate ends the gather (typically the receive_loop on a
        # ConnectionClosed); the outer reconnect loop handles backoff.
        await asyncio.gather(
            _heartbeat_loop(ws, accept),
            _receive_loop(ws, settings),
        )


async def _run(settings: Settings, priv: Ed25519PrivateKey) -> None:
    backoff = RECONNECT_BACKOFF_INITIAL_S
    while True:
        try:
            await _session(settings, priv)
            backoff = RECONNECT_BACKOFF_INITIAL_S
        except HandshakeFailed:
            logger.warning("retry_after_reject in_s=%.1f", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
        except (
            ConnectionRefusedError,
            websockets.ConnectionClosed,
            websockets.WebSocketException,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            logger.warning("disconnected error=%s retry_in_s=%.1f", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="potluck-contributor",
        description=(
            "Potluck contributor sidecar. Connects to the coordinator, "
            "attests with an Ed25519 keypair, and heartbeats. No inference "
            "yet — that's m3+."
        ),
    )
    p.add_argument(
        "--verbose", "-v", action="store_true", help="DEBUG-level logging"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    settings = get_settings()

    if not settings.binary_hash:
        logger.error(
            "missing POTLUCK_BINARY_HASH — set it to the SHA256 of the "
            "current contributor binary, which must be present in the "
            "coordinator's contributor_releases table"
        )
        sys.exit(2)

    if "REPLACE_WITH" in settings.coordinator_url:
        logger.error(
            "POTLUCK_COORDINATOR_URL still contains the placeholder. "
            "Edit your .env (or the env var) with the dev Mac's actual "
            "LAN/Tailscale IP. Got: %s",
            settings.coordinator_url,
        )
        sys.exit(2)

    priv = ensure_keypair(settings.key_dir)
    pub_b64 = public_key_b64(priv.public_key())
    logger.info(
        "contributor_starting member_id=%s peer_pubkey=%s binary_hash=%s…",
        settings.member_id,
        pub_b64,
        settings.binary_hash[:16],
    )

    try:
        asyncio.run(_run(settings, priv))
    except KeyboardInterrupt:
        logger.info("shutdown_requested")


if __name__ == "__main__":
    main()
