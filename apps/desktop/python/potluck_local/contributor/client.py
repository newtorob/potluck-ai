"""Contributor loop: WebSocket client + lifecycle.

Connects to the coordinator's /peers/connect endpoint, completes the
Ed25519 attestation handshake, then sends periodic heartbeats and serves
dispatched jobs. Reconnects on any failure with exponential backoff.

Two callers share this module:

  * ``python -m potluck_local.contributor`` (``main()`` below): the
    standalone bench-fleet client. Settings come from POTLUCK_* environment
    variables (see config.py); jobs are served by the standalone engines in
    inference.py.
  * ``potluck_local.services.contributor_service.ContributorService``: the
    in-process contributor the desktop app runs when the Contribute compute
    toggle is on. It supplies its own hooks (jobs served through the
    sidecar runtime, capacity from the runtime, member session headers).

``run_contributor_loop`` is the shared core. Everything a caller can vary
travels in ``LoopHooks``; the loop itself owns the protocol, the backoff,
the in-flight job cap and the stop semantics (set ``stop`` and the loop
closes its socket and returns).

Architectural invariant (see manifesto §10.3): this loop must never write
prompt content or member-identifying data to disk, logs, or telemetry. Log
lines carry counters and ids only.
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
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

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
from potluck_local.contributor.telemetry import (
    Capacity,
    full_hardware_summary,
    probe_capacity,
)

logger = logging.getLogger("potluck.contributor")

# Module version — embedded in attestation, persisted by coordinator into
# peer_heartbeats.client_version. Bump when the wire protocol or
# heartbeat shape changes.
CLIENT_VERSION = "0.0.2-dev"

# How long to wait for an accept/reject after sending attestation.
HANDSHAKE_RESPONSE_TIMEOUT_S = 10.0

# How often a connected peer compares what it can serve now with what it
# told the coordinator at the handshake.
CAPABILITY_CHECK_INTERVAL_S = 5.0

# Reconnect backoff bounds.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 60.0
# A session that was accepted and then lasted this long was healthy: the failure
# that ends it starts the backoff over instead of continuing from where earlier
# failures left it.
HEALTHY_SESSION_S = 30.0
# A reconnect the engine asked for itself (what it can serve changed, so it must
# attest again) waits only this long and does not grow the backoff.
REATTEST_PAUSE_S = 0.25
# How long the engine may take, before connecting, to get its circle listener
# ready for the first attestation. Past it, the attestation goes without.
CIRCLE_PREPARE_TIMEOUT_S = 1.5


class HandshakeFailed(Exception):
    """Wraps a coordinator reject — caller treats this as fatal vs.
    transient based on context."""


# Type aliases for the hook signatures. `ws` is the live websockets client
# connection; handlers send JobToken/JobComplete/JobError frames on it.
JobHandler = Callable[[Any, JobAssignedMessage], Awaitable[None]]
CapacityProbe = Callable[[], Awaitable[Capacity]]
CapabilitiesProvider = Callable[[], Awaitable[list[CapabilityEntry]]]
HardwareSummaryProvider = Callable[[], dict[str, Any]]
HeadersProvider = Callable[[], list[tuple[str, str]] | None]
StateCallback = Callable[..., None]


@dataclass
class LoopHooks:
    """Everything a caller of ``run_contributor_loop`` can vary."""

    job_handler: JobHandler
    capacity_probe: CapacityProbe
    capabilities_provider: CapabilitiesProvider
    hardware_summary_provider: HardwareSummaryProvider
    headers_provider: HeadersProvider
    max_concurrent_jobs: int | None = 1
    # Who this peer serves beyond its owner: "circle" and/or "pool". None
    # names no audience and serves both, as peers did before the choice.
    audiences: list[str] | None = None
    # on_state(event, **fields). Events: connecting, connected, heartbeat,
    # rejected, disconnected, job_started, job_finished, stopped. Never
    # carries prompt content or member ids.
    on_state: StateCallback | None = None
    # Direct circle links. Both stay None for the standalone client, which then
    # behaves exactly as before them.
    #   circle_attest() -> (mesh node id, circle listener port) while the
    #     listener is up, else None. Read at every handshake and compared by the
    #     capability watch, so a listener that comes up (or goes) is re-attested.
    #   direct_jobs() -> how many direct circle requests are running now. They
    #     share the job cap with relay jobs and count in the heartbeat.
    #   circle_prepare() -> brings the listener up, if its local conditions hold,
    #     before the connection is made, so the first attestation already carries
    #     its node and port. Bounded in time and soft: a failure or a timeout means
    #     the attestation goes without, and the capability watch catches up.
    circle_attest: Callable[[], tuple[str, int] | None] | None = None
    direct_jobs: Callable[[], int] | None = None
    circle_prepare: Callable[[], Awaitable[None]] | None = None


@dataclass
class JobTracker:
    """Jobs currently being served on this connection."""

    in_flight: set[uuid.UUID] = field(default_factory=set)

    @property
    def count(self) -> int:
        return len(self.in_flight)


def detect_platform() -> str:
    """Cross-platform identifier, e.g. 'darwin-arm64', 'linux-x86_64'.

    Note Windows reports machine() as 'AMD64' rather than 'x86_64' — we
    keep what platform.machine() returns rather than normalizing, so the
    string is faithful to what the underlying OS calls itself. Coordinator
    treats this as informational, not a routing key.
    """
    return f"{_platform.system().lower()}-{_platform.machine().lower()}"


def detect_hardware_summary() -> dict[str, object]:
    """Hardware facts for attestation: the legacy four keys plus whatever
    the sidecar's detector finds (gpu_name, gpu_vram_gb, recommended_tier,
    core counts, total RAM). See telemetry.full_hardware_summary."""
    return full_hardware_summary()


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
    """Enumerate the models this standalone peer is willing to serve.

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


def _emit(hooks: LoopHooks, event: str, **fields: Any) -> None:
    """Deliver a state event to the caller. A callback that raises must
    never take the connection down with it."""
    if hooks.on_state is None:
        return
    try:
        hooks.on_state(event, **fields)
    except Exception:  # noqa: BLE001
        logger.debug("on_state_callback_failed event=%s", event, exc_info=True)


def _direct_jobs(hooks: LoopHooks) -> int:
    """Direct circle requests running now. They count against the job cap and in
    the heartbeat; a hook that fails counts as none."""
    if hooks.direct_jobs is None:
        return 0
    try:
        return max(0, int(hooks.direct_jobs()))
    except Exception:  # noqa: BLE001
        return 0


@dataclass
class _SessionInfo:
    """What one session tells the loop that is not in its exit: whether the coordinator
    accepted it (and when), and whether the engine itself ended it to attest again."""

    accepted_at: float | None = None
    reattest: bool = False

    def healthy(self) -> bool:
        return self.accepted_at is not None and time.monotonic() - self.accepted_at >= HEALTHY_SESSION_S


async def _prepare_circle(hooks: LoopHooks) -> None:
    """Give the engine a moment to get its circle listener ready before the first
    attestation. Never fails and never takes longer than CIRCLE_PREPARE_TIMEOUT_S."""
    if hooks.circle_prepare is None:
        return
    try:
        async with asyncio.timeout(CIRCLE_PREPARE_TIMEOUT_S):
            await hooks.circle_prepare()
    except Exception:  # noqa: BLE001 - includes the timeout; the attestation just goes without
        logger.debug("circle_prepare_skipped", exc_info=True)


def _circle_link(hooks: LoopHooks) -> tuple[str, int] | None:
    """What to attest for direct circle links: (node id, listener port) or None."""
    if hooks.circle_attest is None:
        return None
    try:
        return hooks.circle_attest()
    except Exception:  # noqa: BLE001
        return None


# The two attestation fields that exist only for direct circle links. They are
# left out of the message unless the listener is up, so a coordinator from
# before links sees the bytes it always did.
_LINK_FIELDS = {"mesh_node_id", "circle_port"}


async def _do_handshake(
    ws: Any,
    settings: Settings,
    priv: Ed25519PrivateKey,
    hooks: LoopHooks,
    link: tuple[str, int] | None = None,
) -> tuple[AcceptMessage, list[CapabilityEntry]]:
    # 1) receive challenge. A peer with no valid sign-in gets a reject here
    # in place of the challenge, so that case is a reject too.
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_RESPONSE_TIMEOUT_S)
    try:
        first = json.loads(raw)
    except json.JSONDecodeError:
        raise HandshakeFailed("unreadable first message from coordinator") from None
    if isinstance(first, dict) and first.get("type") == "reject":
        raise HandshakeFailed(str(first.get("reason") or "rejected"))
    try:
        challenge = ChallengeMessage.model_validate(first)
    except ValidationError:
        raise HandshakeFailed("unexpected first message from coordinator") from None
    nonce_bytes = base64.urlsafe_b64decode(
        challenge.nonce + "=" * (-len(challenge.nonce) % 4)
    )

    # 2) sign + send attest
    sig = priv.sign(nonce_bytes)
    capabilities = await hooks.capabilities_provider()
    attest = AttestationPayload(
        member_id=settings.member_id,
        peer_public_key=public_key_b64(priv.public_key()),
        binary_hash=settings.binary_hash,
        client_version=CLIENT_VERSION,
        platform=detect_platform(),
        hostname=socket.gethostname(),
        hardware_summary=hooks.hardware_summary_provider(),
        nonce_signature=base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
        capabilities=capabilities,
        audiences=hooks.audiences,
        mesh_node_id=link[0] if link else None,
        circle_port=link[1] if link else None,
    )
    if capabilities:
        logger.info(
            "advertising_capabilities slugs=%s",
            [c.model_slug for c in capabilities],
        )
    await ws.send(attest.model_dump_json(exclude=None if link else _LINK_FIELDS))

    # 3) receive accept or reject
    raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_RESPONSE_TIMEOUT_S)
    msg = json.loads(raw)
    msg_type = msg.get("type")
    if msg_type == "accept":
        accept = AcceptMessage.model_validate(msg)
        if not _audiences_honoured(hooks.audiences, accept.audiences):
            # A coordinator that does not enforce the limit would hand this
            # machine jobs its owner never agreed to. Leave, serving nothing.
            raise HandshakeFailed("this coordinator cannot limit who the machine serves")
        return accept, capabilities
    if msg_type == "reject":
        reject = RejectMessage.model_validate(msg)
        raise HandshakeFailed(reject.reason)
    raise HandshakeFailed(f"unexpected post-attestation message: {msg_type}")


async def _heartbeat_loop(
    ws: Any,
    accept: AcceptMessage,
    hooks: LoopHooks,
    tracker: JobTracker,
) -> None:
    """Send a heartbeat every `heartbeat_interval_s`. Returns when the
    connection closes; caller handles reconnect.

    current_jobs is the loop's own in-flight count (plus the direct circle
    requests the engine reports), never a caller-supplied number: the
    coordinator takes max(live, reported) so a low report can only understate
    load, and this loop is the authority on what it is serving.
    """
    while True:
        await asyncio.sleep(accept.heartbeat_interval_s)
        try:
            capacity = await hooks.capacity_probe()
        except Exception:  # noqa: BLE001
            logger.debug("capacity_probe_failed", exc_info=True)
            capacity = Capacity()
        current_jobs = tracker.count + _direct_jobs(hooks)
        hb = HeartbeatMessage(
            peer_id=accept.peer_id,
            session_id=accept.session_id,
            timestamp=datetime.now(timezone.utc),
            current_jobs=current_jobs,
            gpu_utilization=capacity.gpu_utilization,
            vram_available_gb=capacity.vram_available_gb,
            max_concurrent_jobs=hooks.max_concurrent_jobs,
        )
        await ws.send(hb.model_dump_json())
        logger.debug("heartbeat_sent peer_id=%s current_jobs=%d", accept.peer_id, current_jobs)
        _emit(hooks, "heartbeat", current_jobs=current_jobs)


# m3 stub: per-token "inference" delay so the streaming feels real on the
# coordinator + desktop side.
_STUB_TOKEN_DELAY_S = 0.03


async def _handle_real_job(
    ws: Any,
    job: JobAssignedMessage,
    settings: Settings,
) -> None:
    """Standalone real-inference handler (Phase G m3 step 5).

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
        # Usage dashboard: record that this machine SERVED a network request.
        # The wire deliberately hides whether it was circle or pool, so served
        # rows are recorded as "pool" with NO peer identity. Failure-tolerant.
        try:
            from potluck_local.services.usage_store import record_usage_event
            await record_usage_event(
                direction="served",
                scope="pool",
                model_slug=serve_slug,
                output_tokens=token_index,
                duration_ms=duration_ms,
                ok=True,
            )
        except Exception:
            logger.debug("usage_record_served_failed job_id=%s", job.job_id, exc_info=True)
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
    ws: Any,
    job: JobAssignedMessage,
) -> None:
    """m3 stub inference handler — echoes the prompt back as tokens.

    Privacy invariant: this function MUST NOT log job.prompt content,
    write it to disk, or persist it anywhere.
    """
    started = time.monotonic()
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
        logger.info(
            "stub_job_complete job_id=%s output_tokens=%d duration_ms=%d reason=%s",
            job.job_id, max_chars, duration_ms, finish_reason,
        )
    except websockets.ConnectionClosed:
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


async def _decline_at_capacity(ws: Any, job: JobAssignedMessage, cap: int, in_flight: int) -> None:
    err = JobErrorMessage(
        job_id=job.job_id,
        error_category="declined",
        error_message=f"at capacity: {in_flight} of {cap} jobs in flight",
    )
    try:
        await ws.send(err.model_dump_json())
    except websockets.ConnectionClosed:
        pass
    logger.info(
        "job_declined_at_capacity job_id=%s in_flight=%d cap=%d", job.job_id, in_flight, cap,
    )


async def _receive_loop(
    ws: Any,
    hooks: LoopHooks,
    tracker: JobTracker,
) -> None:
    """Read incoming server-sent messages and dispatch by type.

    Job handlers are spawned as separate tasks so token streaming for one
    job doesn't block reading the next message. Every spawned handler task
    is tracked and, in the `finally` below, cancelled + awaited before this
    function returns, so a disconnect never leaves a handler driving a
    model against a socket that is gone.

    The in-flight cap is enforced HERE, once, for every caller: a job that
    arrives while ``max_concurrent_jobs`` are already running is declined on
    the wire (category ``declined``) so the coordinator reroutes it. Direct
    circle requests count against the same cap.
    """
    background_tasks: set[asyncio.Task] = set()

    def _finish(job_id: uuid.UUID, task: asyncio.Task) -> None:
        tracker.in_flight.discard(job_id)
        background_tasks.discard(task)
        ok = not task.cancelled() and task.exception() is None
        _emit(hooks, "job_finished", job_id=str(job_id), ok=ok)

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
                cap = hooks.max_concurrent_jobs
                running = tracker.count + _direct_jobs(hooks)
                if cap is not None and running >= cap:
                    await _decline_at_capacity(ws, job, cap, running)
                    continue
                tracker.in_flight.add(job.job_id)
                _emit(hooks, "job_started", job_id=str(job.job_id))
                task = asyncio.create_task(hooks.job_handler(ws, job))
                background_tasks.add(task)
                task.add_done_callback(lambda t, jid=job.job_id: _finish(jid, t))
            else:
                logger.debug("server_message_unhandled type=%s", msg_type)
    finally:
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)


async def _stop_watch(ws: Any, stop: asyncio.Event) -> None:
    """Close the socket the moment the caller asks the loop to stop. The
    receive loop then ends with ConnectionClosed and the session unwinds."""
    await stop.wait()
    await ws.close()


_ALL_AUDIENCES = frozenset({"circle", "pool"})


def _audiences_honoured(asked: list[str] | None, enforced: list[str] | None) -> bool:
    """Whether the coordinator will serve this peer to no one beyond those
    it asked for. Asking for everyone needs no confirmation; any narrower
    choice has to be echoed back, with nothing added."""
    if asked is None or frozenset(asked) >= _ALL_AUDIENCES:
        return True
    return enforced is not None and frozenset(enforced) <= frozenset(asked)


def _slugs(capabilities: list[CapabilityEntry]) -> frozenset[str]:
    return frozenset(c.model_slug for c in capabilities)


async def _capability_watch(
    ws: Any,
    hooks: LoopHooks,
    tracker: JobTracker,
    advertised: frozenset[str],
    advertised_link: tuple[str, int] | None = None,
    reattest: asyncio.Event | None = None,
) -> None:
    """End the session once what this machine can serve has changed.

    Capabilities travel only in the attestation, so a model loaded, swapped
    or unloaded after connecting (or a sharing window opening) reaches the
    coordinator only through a new handshake. So does the circle listener's
    node and port (direct circle links). Acts on two matching checks
    and an idle connection, so a brief flap or a job in flight never costs
    a reconnect."""
    pending: tuple[frozenset[str], tuple[str, int] | None] | None = None
    baseline = (advertised, advertised_link)
    while True:
        await asyncio.sleep(CAPABILITY_CHECK_INTERVAL_S)
        try:
            current = (_slugs(await hooks.capabilities_provider()), _circle_link(hooks))
        except Exception:  # noqa: BLE001
            continue
        if current == baseline:
            pending = None
            continue
        if current != pending:
            pending = current
            continue
        if tracker.count:
            continue
        logger.info("capabilities_changed reconnecting")
        if reattest is not None:
            reattest.set()  # before the close, so the loop sees this was our own doing
        await ws.close()
        return


async def _session(
    settings: Settings,
    priv: Ed25519PrivateKey,
    hooks: LoopHooks,
    stop: asyncio.Event,
    info: _SessionInfo | None = None,
) -> None:
    info = info if info is not None else _SessionInfo()
    logger.info("connecting url=%s", settings.coordinator_url)
    _emit(hooks, "connecting")
    await _prepare_circle(hooks)
    reattest = asyncio.Event()
    async with websockets.connect(
        settings.coordinator_url, extra_headers=hooks.headers_provider()
    ) as ws:
        link = _circle_link(hooks)
        try:
            accept, advertised = await _do_handshake(ws, settings, priv, hooks, link)
        except HandshakeFailed as e:
            logger.error("handshake_rejected reason=%s", e)
            _emit(hooks, "rejected", reason=str(e))
            raise
        info.accepted_at = time.monotonic()
        logger.info(
            "session_opened peer_id=%s session_id=%s heartbeat_interval=%ds",
            accept.peer_id,
            accept.session_id,
            accept.heartbeat_interval_s,
        )
        _emit(
            hooks,
            "connected",
            peer_id=str(accept.peer_id),
            session_id=str(accept.session_id),
            heartbeat_interval_s=accept.heartbeat_interval_s,
            # What the coordinator said about direct circle links, and whether it
            # bound the node this attestation named.
            circle_links=accept.circle_links,
            circle_attested=link is not None,
            circle_bound=accept.mesh_node_id is not None,
        )
        tracker = JobTracker()
        # What the coordinator actually holds of the circle fields. One that did not
        # take them up (older, links off, or it would not bind the node) holds
        # nothing, so the listener going away again is no reason to attest anew.
        held_link = link if accept.circle_links is True and accept.mesh_node_id is not None else None
        # Bidirectional: heartbeat outbound, receive incoming, stop watch.
        # First task to terminate ends the gather (typically the receive
        # loop on a ConnectionClosed); the outer loop handles backoff.
        tasks = [
            asyncio.create_task(_heartbeat_loop(ws, accept, hooks, tracker)),
            asyncio.create_task(_receive_loop(ws, hooks, tracker)),
            asyncio.create_task(_stop_watch(ws, stop)),
            asyncio.create_task(_capability_watch(ws, hooks, tracker, _slugs(advertised), held_link, reattest)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                exc = t.exception() if not t.cancelled() else None
                if exc is not None:
                    if reattest.is_set() and isinstance(exc, websockets.ConnectionClosed):
                        continue  # the close was ours: not a failure
                    raise exc
            info.reattest = reattest.is_set()
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_contributor_loop(
    settings: Settings,
    priv: Ed25519PrivateKey,
    hooks: LoopHooks,
    stop: asyncio.Event | None = None,
) -> None:
    """Connect, serve, reconnect, until ``stop`` is set (or forever).

    Returns normally once ``stop`` is set: an open session is closed first,
    a backoff sleep is cut short, and a handshake in progress finishes or
    fails before the return.
    """
    stop = stop or asyncio.Event()
    backoff = RECONNECT_BACKOFF_INITIAL_S

    async def _sleep_or_stop(seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    while not stop.is_set():
        info = _SessionInfo()
        try:
            await _session(settings, priv, hooks, stop, info)
            backoff = RECONNECT_BACKOFF_INITIAL_S
            if stop.is_set():
                break
            if info.reattest:
                # The engine ended this session itself to attest again. Come
                # straight back, and leave the backoff where it is.
                logger.info("reattesting")
                _emit(hooks, "reattest")
                await _sleep_or_stop(REATTEST_PAUSE_S)
                continue
            logger.warning("session_ended retry_in_s=%.1f", backoff)
            _emit(hooks, "disconnected", error="session_ended")
        except HandshakeFailed:
            logger.warning("retry_after_reject in_s=%.1f", backoff)
        except (
            ConnectionRefusedError,
            websockets.ConnectionClosed,
            websockets.WebSocketException,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            if stop.is_set():
                break
            logger.warning("disconnected error=%s retry_in_s=%.1f", e, backoff)
            _emit(hooks, "disconnected", error=type(e).__name__)
        except Exception as e:  # noqa: BLE001
            # Anything unforeseen is one failed session, never the end of
            # the loop: the toggle is still on and the member expects it to
            # come back.
            if stop.is_set():
                break
            logger.error("session_failed error=%s retry_in_s=%.1f", type(e).__name__, backoff)
            _emit(hooks, "disconnected", error=type(e).__name__)
        if stop.is_set():
            break
        if info.healthy():
            backoff = RECONNECT_BACKOFF_INITIAL_S
        await _sleep_or_stop(backoff)
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
    _emit(hooks, "stopped")


def standalone_hooks(settings: Settings) -> LoopHooks:
    """The hooks the bench-fleet script uses: standalone engines from
    inference.py, GPU capacity from telemetry, headers from the shared
    coordinator token when configured."""

    async def _job_handler(ws: Any, job: JobAssignedMessage) -> None:
        if settings.inference_enabled:
            await _handle_real_job(ws, job, settings)
        else:
            await _handle_stub_job(ws, job)

    async def _capacity() -> Capacity:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, probe_capacity)

    async def _capabilities() -> list[CapabilityEntry]:
        return await _build_capabilities(settings)

    def _headers() -> list[tuple[str, str]] | None:
        if settings.coordinator_token:
            return [("X-Potluck-Coordinator-Token", settings.coordinator_token)]
        return None

    return LoopHooks(
        job_handler=_job_handler,
        capacity_probe=_capacity,
        capabilities_provider=_capabilities,
        hardware_summary_provider=detect_hardware_summary,
        headers_provider=_headers,
        max_concurrent_jobs=settings.max_concurrent_jobs,
    )


async def _run(settings: Settings, priv: Ed25519PrivateKey) -> None:
    await run_contributor_loop(settings, priv, standalone_hooks(settings))


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
            "attests with an Ed25519 keypair, heartbeats, and serves jobs."
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
        from potluck_local.contributor.release import frozen_bundle_dir, resolve_binary_hash

        resolved, source = resolve_binary_hash(None, frozen_bundle_dir())
        settings = settings.model_copy(update={"binary_hash": resolved})
        logger.info("binary_hash_resolved source=%s", source)

    if "REPLACE_WITH" in settings.coordinator_url:
        logger.error(
            "POTLUCK_COORDINATOR_URL still contains the placeholder. "
            "Edit your .env (or the env var) with the coordinator's actual "
            "address. Got: %s",
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
