"""The in-process contributor: what the Contribute compute toggle runs.

One supervised asyncio task inside the sidecar drives the shared loop in
``contributor/client.py`` with hooks that serve coordinator jobs through
the sidecar's own runtime, report capacity from that runtime and the GPU,
and present the member's session as the credential. The task starts when
``network.contributor_mode`` turns on (settings PATCH or app boot), restarts
when the coordinator URL or the job cap changes, and stops when the toggle
turns off or the sidecar shuts down. Stopping closes the socket, which is
what makes the coordinator drain any job still in flight.

Why in-process and not a second executable: Electron already supervises
this process; the app's loaded model is the model the peer serves (no
second copy in memory); the busy signal, benchmarks and rpc_servers
support all live in the runtime; and cancelling one task is a stop the
coordinator observes within a heartbeat.

Direct circle links (lending): while this machine shares with circles, the
owner has not turned direct links off and its mesh sidecar advertises
circle_link_v1 and a mesh address, the service also runs the circle listener
(routers/circle_link.py), bound to that mesh address alone, and attests its node
and port so the coordinator can pick it as a lender. The listener comes up
before the first attestation, so that one already carries the fields; an accept
that does not say circle_links stops it again. Requests that arrive over a link
share this machine's job cap with relay jobs. A failure anywhere in this never
reaches the loop: the relay keeps serving.

Privacy: the member session token is held in memory only, never logged and
never written to disk; status exposes counts, this peer's own id, and the
coordinator host, nothing else. Heartbeats carry capacity only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import websockets

from potluck_local.contributor import client as contributor_client
from potluck_local.contributor.config import Settings as ContributorSettings
from potluck_local.contributor.config import get_settings as get_contributor_settings
from potluck_local.contributor.keys import ensure_keypair, public_key_b64
from potluck_local.contributor.release import frozen_bundle_dir, resolve_binary_hash
from potluck_local.contributor.schemas import (
    CapabilityEntry,
    JobAssignedMessage,
    JobCompleteMessage,
    JobErrorMessage,
    JobTokenMessage,
)
from potluck_local.contributor.telemetry import (
    Capacity,
    full_hardware_summary,
    probe_capacity,
)
from potluck_local.services import acceptance, circle_listener, circle_mesh, inference_errors
from potluck_local.services.circle_gate import CircleRoster, DirectAdmission
from potluck_local.services.settings_manager import PotluckSettings
from potluck_local.services.contribution_policy import admission_reason

logger = logging.getLogger(__name__)

STOP_TIMEOUT_S = 5.0

# How often the circle listener's conditions are looked at when nothing wakes
# the watcher sooner (an accept, a settings change).
CIRCLE_CHECK_INTERVAL_S = 5.0
# After a failed bind or start, wait this long before trying again.
CIRCLE_RETRY_S = 30.0
# The mesh sidecar must fail this many checks in a row before a running listener
# is taken down. A sidecar restart should not cost a reconnect.
CIRCLE_SIDECAR_STRIKES = 3

# What the coordinator's reject reasons mean to the person looking at the
# Network view. Matched by substring on the reason string the coordinator
# sends; anything unrecognised is plain "rejected".
_REJECT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("not registered for any member", "device_key_not_registered"),
    ("registered to a different member", "key_bound_to_other_member"),
    ("not in contributor_releases", "release_not_registered"),
    ("cannot limit who the machine serves", "coordinator_outdated"),
    ("unauthorized", "unauthorized"),
)


def open_pool_enabled() -> bool:
    """Whether this build may serve the open pool.

    Off unless POTLUCK_OPEN_POOL_CONTRIBUTOR=1. Releases leave it off until
    the open pool is ready. Sharing with trusted circles does not need it
    (see sharing_audiences); serving the member's own machines never did
    (the household path reads contributor_mode directly).
    """
    return os.environ.get("POTLUCK_OPEN_POOL_CONTRIBUTOR") == "1"


def sharing_audiences(network: Any) -> list[str] | None:
    """Who this machine serves through the coordinator, as it will attest.
    None: it does not join the coordinator (its owner's own machines only).

    A build without the open pool can still share with trusted circles; it
    just never names the pool. With no choice made, a build keeps what it
    did before the choice existed: everyone where the open pool is switched
    on, own machines only where it is not.
    """
    choice = getattr(network, "contributor_audience", None)
    if choice is None:
        choice = "everyone" if open_pool_enabled() else "mine"
    if choice == "everyone":
        return ["circle", "pool"] if open_pool_enabled() else ["circle"]
    if choice == "circles":
        return ["circle"]
    return None


def reject_category(reason: str) -> str:
    lowered = reason.lower()
    for needle, category in _REJECT_CATEGORIES:
        if needle in lowered:
            return category
    return "rejected"


# Plain ws:// is only for coordinators that the session token cannot leave
# the machine or the local network to reach: loopback, private LAN, and the
# 100.64/10 range the mesh and Tailscale use (encrypted by WireGuard).
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # private LAN (RFC 1918)
    "100.64.0.0/10",                                   # mesh and Tailscale
    "fc00::/7",                                        # IPv6 unique local
))


def _plaintext_ok(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or any(ip in net for net in _LOCAL_NETWORKS if ip.version == net.version)


def ws_url_from_http_base(base: str) -> str:
    """``https://host`` becomes ``wss://host/peers/connect``; ``ws(s)://``
    inputs are accepted as-is (path added if missing).

    The member session bearer rides on this socket, so plain ``ws://`` is
    refused (ValueError) unless the host is loopback, private or mesh.
    """
    parts = urlsplit(base.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None:
        raise ValueError("coordinator URL must use http(s) or ws(s)")
    if scheme == "ws" and not _plaintext_ok(parts.hostname):
        raise ValueError("coordinator URL must use https or wss for a public host")
    path = parts.path.rstrip("/")
    if not path.endswith("/peers/connect"):
        path = f"{path}/peers/connect"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


@dataclass
class ContributorStatus:
    enabled: bool = False
    # stopped | connecting | connected | rejected | error
    state: str = "stopped"
    peer_id: str | None = None
    connected_since: str | None = None
    last_heartbeat_at: str | None = None
    heartbeats_sent: int = 0
    reconnects: int = 0
    jobs_served: int = 0
    jobs_in_flight: int = 0
    last_error: str | None = None
    last_error_detail: str | None = None
    coordinator_host: str | None = None
    binary_hash_source: str | None = None
    max_concurrent_jobs: int | None = None
    # Who this machine serves beyond its owner while connected.
    audiences: list[str] | None = None
    # The port the circle listener is bound to, while it is up (direct circle links).
    circle_port: int | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


LoopRunner = Callable[..., Awaitable[None]]


class ContributorService:
    """Owns the contributor task. One instance per sidecar process."""

    def __init__(
        self,
        *,
        runtime_getter: Callable[[], Any] | None = None,
        loop_runner: LoopRunner | None = None,
        hardware_getter: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        circle_listener_factory: Callable[["ContributorService", str], Any] | None = None,
        circle_status_reader: Callable[[], Awaitable[circle_mesh.SidecarStatus | None]] | None = None,
    ) -> None:
        self._runtime_getter = runtime_getter
        self._loop_runner = loop_runner or contributor_client.run_contributor_loop
        self._hardware_getter = hardware_getter
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._session_token: str | None = None
        self._key_registration_tried = False
        self._running_key: tuple[str, int, tuple[str, ...]] | None = None
        self._status = ContributorStatus()
        self._lock = asyncio.Lock()
        self._last_hooks: contributor_client.LoopHooks | None = None
        self._last_settings: ContributorSettings | None = None
        self._network = None
        # Direct circle links (lending). The relay's jobs in flight, counted from
        # the loop's events, and the direct requests, counted by the admission
        # object, share one cap.
        self._relay_in_flight = 0
        self._admission = DirectAdmission(
            cap=lambda: getattr(self._network, "max_concurrent_jobs", None),
            other_in_flight=lambda: self._relay_in_flight,
        )
        self._roster = CircleRoster()
        self._circle_listener_factory = circle_listener_factory
        self._circle_status_reader = circle_status_reader or circle_mesh.read_status
        self._circle_listener: Any = None
        self._circle_task: asyncio.Task | None = None
        self._circle_wake = asyncio.Event()
        # One reconcile at a time: the watcher and the pre-connect preparation both run it.
        self._circle_lock = asyncio.Lock()
        # What this coordinator's accepts said about circle links: True, False
        # (absent counts as False), or None before any accept in this run.
        self._circle_links: bool | None = None
        # Set while the service is being stopped: the loop may still be preparing a
        # connection, and must not bring a listener up behind the stop.
        self._circle_stopping = False
        # The coordinator accepted this machine but would not bind its node, so a
        # listener could never be used. Cleared by a new sign-in or a restart.
        self._circle_bind_refused = False
        self._circle_attest: tuple[str, int] | None = None
        self._circle_retry_at = 0.0
        self._circle_bad_streak = 0

    # ---- public surface ------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        snapshot = asdict(self._status)
        snapshot["running"] = self.is_running
        reason = admission_reason(self._network) if self._network is not None else 'disabled'
        if reason is None and self._loaded_model_unverified():
            reason = "model_not_verified"
        snapshot["accepting_jobs"] = reason is None and self._status.state == 'connected'
        snapshot["pause_reason"] = reason
        return snapshot

    def _loaded_model_unverified(self) -> bool:
        """A model is loaded and has not passed its answer check, so this machine
        turns other people's requests away."""
        try:
            return self._runtime().answers_verified() is False
        except Exception:  # noqa: BLE001 - status must always answer
            return False

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_member_session(self, token: str | None) -> None:
        """Hold (or clear) the member's session bearer. Read at every
        connect, so a token that arrives while the loop is backing off after
        an unauthorized reject is used on the next attempt."""
        self._session_token = token.strip() if token else None
        # A different sign-in is a different member to register the key for, and
        # may be bound to its node where the last one was not.
        self._key_registration_tried = False
        self._circle_bind_refused = False
        self._poke_circle()
        if self._session_token is None:
            logger.info("contributor_member_session_cleared")
        else:
            logger.info("contributor_member_session_set")

    def has_member_session(self) -> bool:
        return self._session_token is not None

    def member_session_token(self) -> str | None:
        """The held session, for the engine's own coordinator calls."""
        return self._session_token

    async def apply_settings(self, settings: PotluckSettings) -> None:
        """Reconcile the running state with what settings ask for."""
        async with self._lock:
            network = settings.network
            self._network = network
            self._status.enabled = bool(network.contributor_mode)
            audiences = sharing_audiences(network)
            if audiences is None:
                if self.is_running:
                    await self._stop_locked()
                self._status.state = "disabled"
                return
            if not network.contributor_mode:
                if self.is_running:
                    await self._stop_locked()
                else:
                    self._status.state = "stopped"
                return

            from potluck_local.services.pool_routing import coordinator_http_base

            base = coordinator_http_base()
            if not base:
                if self.is_running:
                    await self._stop_locked()
                self._status.state = "error"
                self._status.last_error = "coordinator_not_configured"
                self._status.last_error_detail = None
                self._status.coordinator_host = None
                logger.warning("contributor_not_started reason=coordinator_not_configured")
                return

            try:
                ws_url = ws_url_from_http_base(base)
            except ValueError:
                if self.is_running:
                    await self._stop_locked()
                self._status.state = "error"
                self._status.last_error = "coordinator_insecure"
                self._status.last_error_detail = None
                self._status.coordinator_host = None
                logger.warning("contributor_not_started reason=coordinator_insecure")
                return
            key = (ws_url, int(network.max_concurrent_jobs), tuple(audiences))
            if self.is_running and self._running_key == key:
                # Nothing for the loop to redo, but the direct-links setting may
                # have changed.
                self._poke_circle()
                return
            if self.is_running:
                await self._stop_locked()
            await self._start_locked(key[0], key[1], audiences)

    async def stop(self) -> None:
        async with self._lock:
            if self.is_running:
                await self._stop_locked()
            else:
                self._status.state = "stopped"
                await self._stop_circle_locked()

    # ---- lifecycle ------------------------------------------------------ #

    async def _start_locked(
        self, ws_url: str, max_concurrent_jobs: int, audiences: list[str]
    ) -> None:
        base_settings = get_contributor_settings()
        binary_hash, source = resolve_binary_hash(
            base_settings.binary_hash or None, frozen_bundle_dir()
        )
        settings = base_settings.model_copy(
            update={
                "coordinator_url": ws_url,
                "binary_hash": binary_hash,
                "max_concurrent_jobs": max_concurrent_jobs,
            }
        )
        priv = ensure_keypair(settings.key_dir)
        detected = await self._detect_hardware()
        summary = full_hardware_summary(detected)
        hooks = contributor_client.LoopHooks(
            job_handler=self._serve_job,
            capacity_probe=self._capacity,
            capabilities_provider=self._capabilities,
            hardware_summary_provider=lambda: dict(summary),
            headers_provider=self._headers,
            max_concurrent_jobs=max_concurrent_jobs,
            audiences=list(audiences),
            on_state=self._on_state,
            circle_attest=self.circle_attest,
            direct_jobs=lambda: self._admission.in_flight,
            circle_prepare=self.circle_prepare,
        )
        self._stop = asyncio.Event()
        self._status = ContributorStatus(
            enabled=True,
            state="connecting",
            coordinator_host=urlsplit(ws_url).netloc,
            binary_hash_source=source,
            max_concurrent_jobs=max_concurrent_jobs,
            audiences=list(audiences),
        )
        self._running_key = (ws_url, max_concurrent_jobs, tuple(audiences))
        self._last_hooks = hooks
        self._last_settings = settings
        self._task = asyncio.create_task(
            self._loop_runner(settings, priv, hooks, self._stop),
            name="potluck-contributor",
        )
        self._task.add_done_callback(self._on_task_done)
        self._circle_links = None
        self._circle_stopping = False
        self._circle_bind_refused = False
        self._circle_bad_streak = 0
        self._circle_task = asyncio.create_task(self._circle_watch(), name="potluck-circle-watch")
        logger.info(
            "contributor_started coordinator_host=%s cap=%d binary_hash_source=%s",
            self._status.coordinator_host, max_concurrent_jobs, source,
        )

    async def _stop_locked(self) -> None:
        # No new direct requests while the relay loop winds down.
        await self._stop_circle_locked()
        task, stop = self._task, self._stop
        if stop is not None:
            stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("contributor_stop_timeout; cancelling")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except Exception:  # noqa: BLE001
                pass
        self._task = None
        self._stop = None
        self._running_key = None
        # A connection being prepared while the stop began could have brought the
        # listener up once more; the loop is gone now, so nothing can.
        await self._stop_circle_listener()
        self._status.state = "stopped"
        self._status.jobs_in_flight = 0
        self._status.peer_id = None
        self._status.connected_since = None
        logger.info("contributor_stopped")

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("contributor_loop_crashed error=%s", type(exc).__name__)
            self._status.state = "error"
            self._status.last_error = "loop_crashed"
            self._status.last_error_detail = type(exc).__name__

    # ---- hooks ----------------------------------------------------------- #

    def _runtime(self) -> Any:
        if self._runtime_getter is not None:
            return self._runtime_getter()
        from potluck_local.services.runtime import get_runtime

        return get_runtime()

    async def _detect_hardware(self) -> dict[str, Any] | None:
        try:
            if self._hardware_getter is not None:
                return await self._hardware_getter()
            from potluck_local.routers.peer import _cached_hardware

            return await _cached_hardware()
        except Exception:  # noqa: BLE001
            logger.debug("contributor_hardware_detect_failed", exc_info=True)
            return None

    def _headers(self) -> list[tuple[str, str]] | None:
        if self._session_token:
            return [("Authorization", f"Bearer {self._session_token}")]
        shared = os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip()
        if shared:
            return [("X-Potluck-Coordinator-Token", shared)]
        return None

    async def _capacity(self) -> Capacity:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, probe_capacity)

    async def _capabilities(self) -> list[CapabilityEntry]:
        if self._network is not None and admission_reason(self._network):
            return []
        runtime = self._runtime()
        try:
            loaded = runtime.current_model_id() if runtime.is_loaded() else None
        except Exception:  # noqa: BLE001
            loaded = None
        if not loaded:
            return []
        if not acceptance.lending_allowed(runtime):
            # Do not offer what will be refused. The loop asks again every few
            # seconds and attests anew when this changes, as it does for a model swap.
            return []
        bench = {}
        try:
            bench = runtime.benchmarks().get(loaded) or {}
        except Exception:  # noqa: BLE001
            pass
        return [
            CapabilityEntry(
                model_slug=loaded,
                quantization=contributor_client._quantization_from_slug(loaded),
                tokens_per_second=bench.get("tokens_per_sec"),
                prompt_tokens_per_second=bench.get("prompt_tokens_per_sec"),
            )
        ]

    def _on_state(self, event: str, **fields: Any) -> None:
        st = self._status
        if event == "connecting":
            if st.state != "rejected":
                st.state = "connecting"
        elif event == "connected":
            st.state = "connected"
            st.peer_id = fields.get("peer_id")
            st.connected_since = _now_iso()
            st.last_error = None
            st.last_error_detail = None
            self._on_accept(fields)
        elif event == "heartbeat":
            st.last_heartbeat_at = _now_iso()
            st.heartbeats_sent += 1
            st.jobs_in_flight = int(fields.get("current_jobs", st.jobs_in_flight))
        elif event == "rejected":
            reason = str(fields.get("reason", ""))
            st.state = "rejected"
            st.last_error = reject_category(reason)
            st.last_error_detail = reason[:200]
            st.peer_id = None
            st.connected_since = None
            self._relay_in_flight = 0
            self._circle_links = False  # no fields for a coordinator that turns this machine away
            self._poke_circle()
            if st.last_error == "device_key_not_registered":
                self._schedule_key_registration()
        elif event == "disconnected":
            if st.state == "connected":
                st.reconnects += 1
            if st.state != "rejected":
                st.state = "connecting"
            st.last_error = "unreachable" if st.state == "connecting" else st.last_error
            st.last_error_detail = str(fields.get("error", ""))[:100] or None
            st.peer_id = None
            st.connected_since = None
            st.jobs_in_flight = 0
            # The jobs of a session are per session; a new session starts at none.
            self._relay_in_flight = 0
        elif event == "reattest":
            # The engine ended the session itself to attest again: connecting, not
            # a failure, so no error is recorded.
            if st.state != "rejected":
                st.state = "connecting"
            st.peer_id = None
            st.connected_since = None
            self._relay_in_flight = 0
        elif event == "job_started":
            st.jobs_in_flight += 1
            self._relay_in_flight += 1
        elif event == "job_finished":
            st.jobs_in_flight = max(0, st.jobs_in_flight - 1)
            self._relay_in_flight = max(0, self._relay_in_flight - 1)
        elif event == "stopped":
            st.state = "stopped"
            st.jobs_in_flight = 0
            self._relay_in_flight = 0
            self._circle_links = None

    # ---- direct circle links (lending) ----------------------------------- #

    def circle_attest(self) -> tuple[str, int] | None:
        """(mesh node id, listener port) while the circle listener is up, for the
        attestation. None otherwise, which leaves both fields out."""
        return self._circle_attest

    def circle_sharing_refusal(self) -> str | None:
        """Why the circle listener must turn callers away right now, or None.
        Read live on every request and on every recheck of a running one."""
        network = self._network
        if network is None:
            return "not_configured"
        if not getattr(network, "direct_circle_links", True):
            return "links_off"
        audiences = sharing_audiences(network)
        if not audiences or "circle" not in audiences:
            return "not_sharing_circles"
        return admission_reason(network)

    def _poke_circle(self) -> None:
        self._circle_wake.set()

    def _on_accept(self, fields: dict[str, Any]) -> None:
        """Take in what the coordinator's accept said about circle links."""
        links = fields.get("circle_links") is True  # absent means "not supported"
        self._circle_links = links
        if links and fields.get("circle_attested"):
            if fields.get("circle_bound"):
                self._circle_bind_refused = False
            else:
                # The coordinator would not bind the node this machine named. A
                # listener could never be picked, and starting it again would only
                # make the attestation flap.
                if not self._circle_bind_refused:
                    logger.warning("circle_binding_refused listener_stopped")
                self._circle_bind_refused = True
        self._poke_circle()

    def _circle_wanted(self) -> bool:
        """The conditions the listener runs under that this service can see: the
        loop is running, the owner shares with circles and has not turned direct
        links off, the engine is reachable from the mesh at all, and no accept
        has said this coordinator does not broker links (before any accept it is
        not known, and the listener comes up at once so the first attestation
        carries it; a listener nobody can have a link to does no harm). The
        sidecar's feature and mesh address are checked by the caller."""
        network = self._network
        if network is None or not self.is_running or self._circle_stopping:
            return False
        if not getattr(network, "direct_circle_links", True):
            return False
        audiences = sharing_audiences(network)
        if not audiences or "circle" not in audiences:
            return False
        if circle_listener.host_is_loopback(circle_listener.bind_host()):
            return False  # POTLUCK_BIND_HOST=127.0.0.1: this machine serves no one else
        return self._circle_links is not False and not self._circle_bind_refused

    async def circle_prepare(self) -> None:
        """Called by the loop before it connects: bring the listener up if its
        conditions hold, so the attestation about to be made carries its port. The
        loop bounds this in time and ignores any failure."""
        async with self._circle_lock:
            await self._reconcile_circle()

    async def _circle_watch(self) -> None:
        """Keep the circle listener in step with its conditions. Nothing here may
        end the task or reach the loop: it only starts and stops the listener."""
        while True:
            self._circle_wake.clear()
            try:
                async with self._circle_lock:
                    await self._reconcile_circle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("circle_reconcile_failed error=%s", type(exc).__name__)
            try:
                # asyncio.timeout, not wait_for: before Python 3.12 wait_for can lose
                # a cancellation that arrives as its inner wait finishes, and this
                # task must end when it is cancelled.
                async with asyncio.timeout(CIRCLE_CHECK_INTERVAL_S):
                    await self._circle_wake.wait()
            except TimeoutError:
                pass

    async def _reconcile_circle(self) -> None:
        if not self._circle_wanted():
            self._circle_bad_streak = 0
            await self._stop_circle_listener()
            return
        status = await self._circle_status_reader()
        if status is not None and not (status.supports_links and circle_mesh.usable_listen_address(status.mesh_ip)):
            # Read fine, and it offers no links, has no node id, or has no mesh
            # address to bind to: that is an answer, not a blip. Without an address
            # there is no listener (never 0.0.0.0), only the relay.
            self._circle_bad_streak = 0
            await self._stop_circle_listener()
            return
        if status is None:
            # Could not be read. A restart of the sidecar must not cost a reconnect.
            self._circle_bad_streak += 1
            if self._circle_bad_streak >= CIRCLE_SIDECAR_STRIKES:
                await self._stop_circle_listener()
            return
        self._circle_bad_streak = 0
        host = str(status.mesh_ip)
        listener = self._circle_listener
        if listener is not None and (not listener.running or listener.host != host):
            # It died on its own, or the mesh address changed (the sidecar enrolled
            # again): start afresh below, on the address there is now.
            await self._stop_circle_listener()
            listener = None
        if listener is None:
            if time.monotonic() < self._circle_retry_at:
                return
            await self._start_circle_listener(str(status.node_id), host)
            return
        # Up: keep the attested node in step with the sidecar's (it can re-enroll).
        port = listener.port
        self._circle_attest = (str(status.node_id), port) if port else None

    async def _start_circle_listener(self, node_id: str, host: str) -> None:
        try:
            listener = self._make_circle_listener(host)
            port = await listener.start()
        except circle_listener.AddressNotAvailable:
            # The sidecar reports an address the machine does not carry yet (just
            # enrolled, or the interface is still coming up): ask again at the next check.
            logger.info("circle_listener_waiting_for_address")
            return
        except Exception as exc:  # noqa: BLE001 - a bind failure must never reach the loop
            self._circle_retry_at = time.monotonic() + CIRCLE_RETRY_S
            logger.warning("circle_listener_start_failed error=%s", type(exc).__name__)
            return
        self._circle_listener = listener
        self._circle_attest = (node_id, port)
        self._status.circle_port = port
        logger.info("circle_listener_up port=%d", port)

    def _make_circle_listener(self, host: str) -> Any:
        if self._circle_listener_factory is not None:
            return self._circle_listener_factory(self, host)
        from potluck_local.routers.circle_link import CircleDeps, create_circle_app

        return circle_listener.CircleListener(
            create_circle_app(
                CircleDeps(
                    roster=self._roster,
                    admission=self._admission,
                    sharing_refusal=self.circle_sharing_refusal,
                    max_tokens_limit=lambda: getattr(self._network, "contributor_max_tokens", None),
                    runtime=self._runtime,
                )
            ),
            host=host,
        )

    async def _stop_circle_listener(self) -> None:
        listener, self._circle_listener = self._circle_listener, None
        self._circle_attest = None
        self._status.circle_port = None
        if listener is not None:
            try:
                await listener.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("circle_listener_stop_failed error=%s", type(exc).__name__)

    async def _stop_circle_locked(self) -> None:
        self._circle_stopping = True
        watcher, self._circle_task = self._circle_task, None
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await self._stop_circle_listener()
        self._circle_links = None

    def _schedule_key_registration(self) -> None:
        """The coordinator does not know this machine yet. The desktop app
        registers the key when sharing is switched on, but a machine run from
        the CLI has nothing to do that, so the engine does it once per
        sign-in. The loop's next retry then attests normally."""
        if self._key_registration_tried or not self._session_token:
            return
        self._key_registration_tried = True
        try:
            asyncio.get_running_loop().create_task(
                self._register_device_key(), name="potluck-contributor-register-key"
            )
        except RuntimeError:  # no running loop (called from sync code in tests)
            self._key_registration_tried = False

    async def _register_device_key(self) -> None:
        import socket

        import httpx

        from potluck_local.services import pool_routing

        try:
            base = pool_routing.coordinator_http_base()
            token = self._session_token
            if not token or not pool_routing._session_may_travel(base):
                return
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            settings = self._last_settings or get_contributor_settings()
            key = public_key_b64(ensure_keypair(settings.key_dir).public_key())
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base}/me/device-keys",
                    json={"public_key": key, "label": socket.gethostname()[:64] or None},
                    headers={**headers, "Accept": "application/json"},
                )
            # 201: registered. 409: already registered, to this member or
            # another; the next attestation says which. Never log the key.
            logger.info("contributor_device_key_registration status=%d", resp.status_code)
        except Exception:  # noqa: BLE001
            logger.warning("contributor_device_key_registration_failed", exc_info=True)

    def _served_scope(self) -> str:
        """What a job served for someone else counts as. The wire does not
        say whether a job came from a circle or the pool, but a machine that
        never offered itself to the pool can only have served a circle."""
        audiences = self._status.audiences
        return "circle" if audiences is not None and "pool" not in audiences else "pool"

    async def _serve_job(self, ws: Any, job: JobAssignedMessage) -> None:
        """Serve one coordinator job through the sidecar runtime.

        Declines (so the coordinator reroutes) when no model is loaded or the
        loaded model is not the one requested. Streams deltas as job_token
        frames and finishes with job_complete. Never logs content.
        """
        network = self._network
        reason = admission_reason(network) if network is not None else None
        if reason or (network is not None and network.contributor_max_tokens is not None
                      and job.sampling.max_tokens > network.contributor_max_tokens):
            await self._send(ws, JobErrorMessage(job_id=job.job_id, error_category='declined',
                error_message='Contributor admission policy does not allow this job.'))
            return
        runtime = self._runtime()
        loaded = runtime.current_model_id() if runtime.is_loaded() else None
        if loaded is None:
            await self._send(ws, JobErrorMessage(
                job_id=job.job_id, error_category="model_not_loaded",
                error_message="no model loaded on this peer",
            ))
            return
        if loaded != job.model_slug:
            await self._send(ws, JobErrorMessage(
                job_id=job.job_id, error_category="declined",
                error_message=f"this peer serves '{loaded}', not '{job.model_slug}'",
            ))
            logger.info("contributor_job_declined job_id=%s reason=model_mismatch", job.job_id)
            return
        if not acceptance.lending_allowed(runtime):
            # Not verified on this machine: the coordinator reroutes. Fixed text.
            await self._send(ws, JobErrorMessage(
                job_id=job.job_id, error_category="declined",
                error_message=inference_errors.other_machine_text(inference_errors.MODEL_NOT_VERIFIED),
            ))
            logger.info("contributor_job_declined job_id=%s reason=model_not_verified", job.job_id)
            return

        if job.messages:
            messages = [{"role": m.role, "content": m.content} for m in job.messages]
        else:
            messages = [{"role": "user", "content": job.prompt}]
        started = time.monotonic()
        token_index = 0
        try:
            async for chunk in runtime.stream_chat_raw(
                messages,
                temperature=job.sampling.temperature,
                max_tokens=job.sampling.max_tokens,
                stop=job.sampling.stop_sequences,
            ):
                if isinstance(chunk, dict) and "__error__" in chunk:
                    if chunk.get("__category__") == inference_errors.GPU_OUT_OF_MEMORY:
                        # The coordinator's own category for this. The text is
                        # fixed; the engine's message never leaves this machine.
                        await self._send(ws, JobErrorMessage(
                            job_id=job.job_id, error_category="oom",
                            error_message=inference_errors.other_machine_text(inference_errors.GPU_OUT_OF_MEMORY),
                        ))
                        return
                    raise RuntimeError(str(chunk["__error__"]))
                delta = None
                try:
                    delta = chunk["choices"][0].get("delta", {}).get("content")
                except (KeyError, IndexError, AttributeError, TypeError):
                    delta = None
                if not delta:
                    continue
                await ws.send(JobTokenMessage(
                    job_id=job.job_id, token_index=token_index, token_text=delta,
                ).model_dump_json())
                token_index += 1
            duration_ms = int((time.monotonic() - started) * 1000)
            finish_reason = "length" if token_index >= job.sampling.max_tokens else "stop"
            await ws.send(JobCompleteMessage(
                job_id=job.job_id, output_tokens=token_index,
                duration_ms=duration_ms, finish_reason=finish_reason,
            ).model_dump_json())
            self._status.jobs_served += 1
            logger.info(
                "contributor_job_complete job_id=%s output_tokens=%d duration_ms=%d reason=%s",
                job.job_id, token_index, duration_ms, finish_reason,
            )
            try:
                from potluck_local.services.usage_store import record_usage_event

                await record_usage_event(
                    direction="served", scope=self._served_scope(), model_slug=loaded,
                    output_tokens=token_index, duration_ms=duration_ms, ok=True,
                )
            except Exception:  # noqa: BLE001
                logger.debug("usage_record_served_failed job_id=%s", job.job_id, exc_info=True)
        except websockets.ConnectionClosed:
            logger.warning("contributor_job_aborted_connection_closed job_id=%s", job.job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The type name only. A traceback or the message can quote the
            # prompt or the output of the member whose job this is.
            logger.error(
                "contributor_job_internal_error job_id=%s error=%s",
                job.job_id, type(exc).__name__,
            )
            await self._send(ws, JobErrorMessage(
                job_id=job.job_id, error_category="internal",
                error_message=type(exc).__name__,
            ))

    @staticmethod
    async def _send(ws: Any, frame: Any) -> None:
        try:
            await ws.send(frame.model_dump_json())
        except websockets.ConnectionClosed:
            pass


_service: ContributorService | None = None


def get_contributor_service() -> ContributorService:
    global _service
    if _service is None:
        _service = ContributorService()
    return _service


def set_contributor_service_for_tests(service: ContributorService | None) -> None:
    global _service
    _service = service
