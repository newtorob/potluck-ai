"""Direct circle links: brokering, renewing, ending and revoking.

A circle request used to be relayed through the coordinator. A link lets the
requester's machine and a lender's machine, in two different households, talk
directly. The coordinator picks the lender, mints a short-lived token, and
tells both machines' mesh sidecars to add the other as a restricted WireGuard
peer (circle_peer_add on /mesh/signal). The request itself then goes machine to
machine. The relay stays as the fallback and is not touched here.

What this module decides:

* Lender choice is the relay's own: select_peer_for_request with the circle's
  lender ids, so the requester's machines stay excluded and each machine's
  sharing audience is honoured. A circle request is never answered from the open
  pool. On top of that the lender must be bound to a mesh node whose sidecar is
  connected and advertises circle_link_v1.
* The token is 32 random bytes, urlsafe base64, returned once to the requester.
  Only its SHA-256 is stored and forwarded (to the lender's sidecar, which hands
  it to the lender engine to check).
* One live link exists per (requester node, lender node); a repeat create for
  the pair supersedes the old row.
* A link lives circle_link_ttl_s seconds (default 10 minutes) and is kept alive
  by renewals. It ends at once when the requester or lender leaves or is removed
  from the circle, the circle is deleted, a machine is removed, or the lender
  stops sharing with circles; and 15 seconds after the lender's engine
  disconnects. A sweeper ends lapsed links and deletes rows 24 hours after they
  end.
* Setup waits up to 2 seconds for both sidecars to acknowledge. A missing
  acknowledgement revokes the link.

Privacy: a link reaches two households that are not each other's. The messages
this module builds carry no member id, node id, node name, hostname or circle
name (SignalCirclePeerAdd and friends; locked in test_privacy_invariants.py).
Messages go out only through routers.mesh.signal_send, the same _safe_send path
the rest of the mesh signal code uses. Log lines name the link, never a member
or machine.

The module-level dicts (pending acknowledgements, create-rate counters, grace
timers) are per process, like the signal connections and the peer registry.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coordinator.config import get_settings
from coordinator.models.circle_link import CircleLink
from coordinator.models.household import Household
from coordinator.models.mesh_node import MeshNode
from coordinator.schemas.circle_link import (
    CircleLinkCreateRequest,
    CircleLinkCreateResponse,
    CircleLinkDelivered,
    CircleLinkErrorDetail,
    CircleLinkLender,
    CircleLinkRenewResponse,
)
from coordinator.schemas.mesh import (
    CIRCLE_PEER_ACK_REASONS,
    SignalCirclePeerAck,
    SignalCirclePeerAdd,
    SignalCirclePeerExtend,
    SignalCirclePeerRemove,
    filter_local_endpoints,
    parse_ip_port,
    safe_endpoint_or_none,
)
from coordinator.services.circles import lender_member_ids, membership_of
from coordinator.services.peer_registry import (
    PeerConnection,
    get_peer,
    select_peer_for_request,
)

logger = structlog.get_logger(__name__)

# What a sidecar puts in hello.features to say it understands circle_peer_*.
LINK_FEATURE = "circle_link_v1"

# How long a create or renew waits for the sidecars' acknowledgements.
ACK_TIMEOUT_S = 2.0
# How long to try a direct path before the sidecar uses the DERP relay, when the
# transport policy allows direct at all.
DIRECT_WINDOW_MS = 3000
# A lender engine that disconnects keeps its links this long. A transient
# websocket blip, or the reconnect an engine does when its model changes, must
# not cut a request in flight.
LENDER_DISCONNECT_GRACE_S = 15.0
# Per requester member.
MAX_LIVE_LINKS_PER_MEMBER = 8
MAX_CREATES_PER_MINUTE = 30
# Rows are deleted this long after a link ends.
RETENTION = timedelta(hours=24)
# Lenders tried in one create: one retry after a lender's sidecar refuses, plus
# room to skip a lender whose machine record turned out to be stale.
MAX_LENDER_TRIES = 3
# A link may be renewed at most once per this fraction of its lifetime (never
# faster than once a second). A renewal sends a message to BOTH sidecars, one of
# which belongs to another household, so the rate has to be bounded here: an
# honest engine renews at half the lifetime.
RENEW_MIN_GAP_DIVISOR = 10
# On a sidecar's reconnect, links that ended within this window are removed again.
# 30 minutes is the longest a sidecar will hold any grant on its own clock, so a
# remove missed while its signal channel was down is repeated for as long as the
# grant could still be there.
REPLAY_REMOVE_WINDOW = timedelta(minutes=30)

_PROCESS_STARTED = time.monotonic()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def links_enabled() -> bool:
    return get_settings().circle_links_enabled


def _mesh():
    # routers.mesh owns the signal connections and imports this module, so the
    # import is made at call time.
    from coordinator.routers import mesh

    return mesh


def hash_token(token: str) -> str:
    """The hex SHA-256 of the token's ASCII text, exactly as the requester sends
    it in an Authorization: Bearer header. The string is hashed as it is, never
    the bytes it decodes to."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def link_error(
    status_code: int,
    code: str,
    message: str,
    *,
    reason: str | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=CircleLinkErrorDetail(code=code, message=message, reason=reason).model_dump(),
        headers=headers,
    )


# End reasons the sidecars hear as "revoked": every cause the member did not
# choose directly. The row keeps the exact one.
_SIGNAL_REASON = {"ended": "ended", "expired": "expired", "superseded": "superseded"}


def signal_reason(end_reason: str) -> str:
    return _SIGNAL_REASON.get(end_reason, "revoked")


@dataclass(frozen=True)
class EndedLink:
    link_id: uuid.UUID
    requester_node_id: uuid.UUID
    lender_node_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class AckResult:
    # "ready" | "rejected" from the sidecar; "timeout" | "offline" are ours.
    state: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------

# (link_id, node_id, op) -> the future a create or renew is waiting on. The node
# id comes from the authenticated signal connection, never from the message, so
# only the node a message was sent to can answer it: an ack from any other node
# finds nothing to wake.
_pending_acks: dict[tuple[uuid.UUID, uuid.UUID, str], asyncio.Future[AckResult]] = {}


async def handle_ack(node_id: uuid.UUID, raw: object) -> None:
    """A circle_peer_ack arrived on `node_id`'s signal connection. Wakes the
    create or renew waiting for it. Never raises: the signal loop that calls
    this also carries the node's household mesh."""
    try:
        try:
            ack = SignalCirclePeerAck.model_validate(raw)
        except ValidationError:
            logger.info("circle_link_ack_invalid")
            return
        future = _pending_acks.get((ack.link_id, node_id, ack.op))
        if future is None or future.done():
            # Not waited for: the ack of a replayed add, or one that came late.
            logger.debug("circle_link_ack_unsolicited", link_id=str(ack.link_id), state=ack.state)
            return
        reason = None
        if ack.state == "rejected":
            reason = ack.reason if ack.reason in CIRCLE_PEER_ACK_REASONS else "unknown"
        future.set_result(AckResult(ack.state, reason))
    except Exception:
        logger.exception("circle_link_ack_handler_failed")


async def _send(node_id: uuid.UUID, payload: dict) -> bool:
    """One message to one sidecar, bounded: a sidecar that stops reading must not
    hold a request (and the requester machine's create lock) open. Never raises."""
    try:
        return await asyncio.wait_for(_mesh().signal_send(node_id, payload), timeout=ACK_TIMEOUT_S)
    except Exception:
        return False


async def _deliver_and_wait(
    link_id: uuid.UUID, sends: list[tuple[uuid.UUID, dict, str]]
) -> dict[uuid.UUID, AckResult]:
    """Send each (node_id, payload, op) and wait up to ACK_TIMEOUT_S for every
    node's ack. Futures are registered before anything is sent so a fast ack
    cannot be missed. A node that is not connected, or that a send fails for,
    comes back "offline" without waiting."""
    loop = asyncio.get_running_loop()
    futures: dict[uuid.UUID, tuple[tuple[uuid.UUID, uuid.UUID, str], asyncio.Future[AckResult]]] = {}
    for node_id, _payload, op in sends:
        key = (link_id, node_id, op)
        future: asyncio.Future[AckResult] = loop.create_future()
        _pending_acks[key] = future
        futures[node_id] = (key, future)
    results: dict[uuid.UUID, AckResult] = {}
    try:
        sent = await asyncio.gather(*(_send(node_id, payload) for node_id, payload, _op in sends))
        waiting: list[uuid.UUID] = []
        for (node_id, _payload, _op), ok in zip(sends, sent):
            if ok:
                waiting.append(node_id)
            else:
                results[node_id] = AckResult("offline")
        if waiting:
            await asyncio.wait([futures[n][1] for n in waiting], timeout=ACK_TIMEOUT_S)
            for node_id in waiting:
                future = futures[node_id][1]
                results[node_id] = future.result() if future.done() else AckResult("timeout")
    finally:
        for key, future in futures.values():
            _pending_acks.pop(key, None)
            if not future.done():
                future.cancel()
    return results


# ---------------------------------------------------------------------------
# Building the messages
# ---------------------------------------------------------------------------


def _same_public_ip(a: str | None, b: str | None) -> bool:
    pa = parse_ip_port(a) if a else None
    pb = parse_ip_port(b) if b else None
    return pa is not None and pb is not None and pa[0] == pb[0]


def _endpoints_for(own: MeshNode, other: MeshNode, *, direct: bool) -> tuple[str | None, list[str]]:
    """The other machine's address(es) as `own` should be told them. Under the
    relay-only policy neither is ever disclosed. Under direct, the public
    endpoint is shared and the LAN candidates only when both machines report the
    same public address (otherwise two unrelated homes that both use
    192.168.1.0/24 would send handshakes at each other's LAN hosts)."""
    if not direct:
        return None, []
    endpoint = safe_endpoint_or_none(other.last_endpoint)
    local: list[str] = []
    if _same_public_ip(own.last_endpoint, other.last_endpoint):
        local = filter_local_endpoints(_mesh().signal_local_endpoints(other.id))
    return endpoint, local


def build_add(
    *,
    link_id: uuid.UUID,
    role: str,
    own: MeshNode,
    other: MeshNode,
    port: int,
    ttl_s: int,
    token_hash: str,
    direct: bool,
) -> dict:
    """The circle_peer_add for `own`'s sidecar, whose role is `role`; `other` is
    the machine at the other end."""
    endpoint, local = _endpoints_for(own, other, direct=direct)
    return SignalCirclePeerAdd(
        link_id=link_id,
        role=role,
        peer_wg_public_key=other.wg_public_key,
        peer_mesh_ip=str(other.mesh_ip),
        port=port,
        ttl_s=ttl_s,
        token_sha256=token_hash if role == "lender" else None,
        peer_endpoint=endpoint,
        peer_local_endpoints=local,
        direct_window_ms=DIRECT_WINDOW_MS if direct else 0,
    ).model_dump(mode="json")


def build_extend(*, link_id: uuid.UUID, own: MeshNode, other: MeshNode, ttl_s: int, direct: bool) -> dict:
    endpoint, _local = _endpoints_for(own, other, direct=direct)
    return SignalCirclePeerExtend(link_id=link_id, ttl_s=ttl_s, peer_endpoint=endpoint).model_dump(
        mode="json"
    )


def build_remove(link_id: uuid.UUID, end_reason: str) -> dict:
    return SignalCirclePeerRemove(link_id=link_id, reason=signal_reason(end_reason)).model_dump(
        mode="json"
    )


def _remaining_ttl_s(link: CircleLink, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int((link.expires_at - now).total_seconds())


def _renew_after(ttl_s: int) -> int:
    return max(1, ttl_s // 2)


# ---------------------------------------------------------------------------
# Ending links (revocation)
# ---------------------------------------------------------------------------


async def _end_where(db: AsyncSession, *conditions, reason: str) -> list[EndedLink]:
    """End every live link matching `conditions`. Does not commit: callers end
    links in the same transaction as the change that justifies it (so both
    happen or neither), then push the removals after the commit."""
    rows = (
        await db.execute(
            update(CircleLink)
            .where(CircleLink.ended_at.is_(None), *conditions)
            .values(ended_at=datetime.now(timezone.utc), end_reason=reason)
            .returning(CircleLink.id, CircleLink.requester_node_id, CircleLink.lender_node_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    return [EndedLink(r[0], r[1], r[2], reason) for r in rows]


async def end_for_circle_member(
    db: AsyncSession, circle_id: uuid.UUID, member_id: uuid.UUID, reason: str
) -> list[EndedLink]:
    """Links in this circle that the member is on either end of."""
    return await _end_where(
        db,
        CircleLink.circle_id == circle_id,
        (CircleLink.requester_member_id == member_id) | (CircleLink.lender_member_id == member_id),
        reason=reason,
    )


async def end_for_circle(
    db: AsyncSession, circle_id: uuid.UUID, reason: str = "circle_deleted"
) -> list[EndedLink]:
    return await _end_where(db, CircleLink.circle_id == circle_id, reason=reason)


async def end_for_node(
    db: AsyncSession, node_id: uuid.UUID, reason: str = "node_removed"
) -> list[EndedLink]:
    return await _end_where(
        db,
        (CircleLink.requester_node_id == node_id) | (CircleLink.lender_node_id == node_id),
        reason=reason,
    )


async def end_not_allowed_in_circle(
    db: AsyncSession, circle_id: uuid.UUID, reason: str = "member_removed"
) -> list[EndedLink]:
    """End the circle's live links that the circle no longer allows: the
    requester is not a member, or the lender is not among the members who may
    lend to that requester (lender_member_ids, the relay's own rule). Used when
    a legacy machine key is taken out of a circle, where the person behind it
    is not named in the request."""
    rows = (
        await db.execute(
            select(CircleLink.id, CircleLink.requester_member_id, CircleLink.lender_member_id).where(
                CircleLink.circle_id == circle_id, CircleLink.ended_at.is_(None)
            )
        )
    ).all()
    lenders: dict[uuid.UUID, set[uuid.UUID]] = {}
    member_ok: dict[uuid.UUID, bool] = {}
    doomed: dict[str, list[uuid.UUID]] = {}
    for link_id, requester, lender in rows:
        if requester not in member_ok:
            member_ok[requester] = await membership_of(db, circle_id, requester) is not None
            lenders[requester] = (
                await lender_member_ids(db, circle_id, requester) if member_ok[requester] else set()
            )
        if not member_ok[requester]:
            doomed.setdefault("member_left", []).append(link_id)
        elif lender not in lenders[requester]:
            doomed.setdefault(reason, []).append(link_id)
    ended: list[EndedLink] = []
    for why, ids in doomed.items():
        ended += await _end_where(db, CircleLink.id.in_(ids), reason=why)
    return ended


async def guarded_end(
    db: AsyncSession, fn: Callable[..., Awaitable[list[EndedLink]]], *args, **kwargs
) -> list[EndedLink]:
    """Run one of the end_* functions for a route that must not depend on links:
    in a savepoint, with any failure logged and swallowed. A circle leave or a
    machine removal succeeds even if the links table is missing or the update
    fails; the links then lapse on their own clocks and the sweeper's
    reconciliation ends them."""
    try:
        async with db.begin_nested():
            return await fn(db, *args, **kwargs)
    except Exception as e:
        logger.warning("circle_link_revoke_failed", error=type(e).__name__)
        return []


async def push_removed(ended: Iterable[EndedLink]) -> None:
    """Tell both sidecars of each ended link to drop it. Best effort: a sidecar
    that is offline or slow is left to its own clock, and nothing here raises.
    Sent concurrently with a short bound so a revocation does not hold up the
    request that caused it."""
    ended = list(ended)
    if not ended:
        return

    async def _one(node_id: uuid.UUID, payload: dict) -> None:
        if not await _send(node_id, payload):
            logger.debug("circle_link_remove_not_delivered")

    jobs = []
    for e in ended:
        payload = build_remove(e.link_id, e.reason)
        jobs.append(_one(e.requester_node_id, payload))
        jobs.append(_one(e.lender_node_id, payload))
        logger.info("circle_link_ended", link_id=str(e.link_id), reason=e.reason)
    try:
        await asyncio.gather(*jobs)
    except Exception:
        logger.exception("circle_link_push_removed_failed")


# Fire-and-forget tasks (cleanup that must outlive a cancelled request). Held
# here so they are not garbage collected before they finish.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _revoked_meanwhile(db: AsyncSession, link: CircleLink | EndedLink, requester_node_id, lender_node_id) -> str | None:
    """After adds have gone out, was the link ended while they were in flight?

    A revocation (a member leaving, say) that lands between a create's insert and
    its adds sends its removes first; the adds then arrive after them and the
    sidecars hold a link the coordinator has already ended. So after any add is
    sent, look again; if the link is over, send the removes again and say why.
    Returns the end reason, or None if the link is still live."""
    link_id = link.id if isinstance(link, CircleLink) else link.link_id
    row = (
        await db.execute(
            select(CircleLink.ended_at, CircleLink.end_reason).where(CircleLink.id == link_id)
        )
    ).first()
    if row is not None and row[0] is None:
        return None
    reason = row[1] if row is not None else "circle_deleted"  # the row went with its circle or machine
    await push_removed([EndedLink(link_id, requester_node_id, lender_node_id, reason)])
    return reason


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

_create_attempts: dict[uuid.UUID, deque[float]] = {}
# Serialises creates from one requester machine, so two at once cannot each
# supersede the other's row mid-setup.
_create_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _count_create_attempt(member_id: uuid.UUID) -> None:
    now = time.monotonic()
    attempts = _create_attempts.setdefault(member_id, deque())
    while attempts and now - attempts[0] > 60.0:
        attempts.popleft()
    if len(attempts) >= MAX_CREATES_PER_MINUTE:
        retry = max(1, int(60.0 - (now - attempts[0])) + 1)
        raise link_error(
            429,
            "too_many_links",
            "Too many link requests. Wait a moment and try again.",
            headers={"Retry-After": str(retry)},
        )
    attempts.append(now)


async def _owned_node(db: AsyncSession, node_id: uuid.UUID, member_id: uuid.UUID) -> MeshNode | None:
    """The mesh node, if its household belongs to `member_id`."""
    return (
        await db.execute(
            select(MeshNode)
            .join(Household, Household.id == MeshNode.household_id)
            .where(MeshNode.id == node_id, Household.owner_member_id == member_id)
        )
    ).scalar_one_or_none()


async def _lender_node(db: AsyncSession, peer: PeerConnection) -> MeshNode | None:
    """The lender's machine record, re-checked now: the node the engine bound to
    at attestation must still exist and still belong to the engine's member."""
    if peer.mesh_node_id is None:
        return None
    return await _owned_node(db, peer.mesh_node_id, peer.member_id)


def _link_capable(excluded_nodes: set[uuid.UUID]) -> Callable[[PeerConnection], bool]:
    mesh = _mesh()

    def ok(conn: PeerConnection) -> bool:
        node_id = conn.mesh_node_id
        if node_id is None or conn.circle_port is None or node_id in excluded_nodes:
            return False
        return mesh.signal_node_connected(node_id) and LINK_FEATURE in mesh.signal_node_features(
            node_id
        )

    return ok


async def _nodes_of_links(
    db: AsyncSession, member_id: uuid.UUID, link_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """The lender machines behind links the caller names as failed. Links that
    are not the caller's are ignored, so this cannot be used to probe ids."""
    if not link_ids:
        return set()
    rows = (
        await db.execute(
            select(CircleLink.lender_node_id).where(
                CircleLink.id.in_(link_ids), CircleLink.requester_member_id == member_id
            )
        )
    ).all()
    return {r[0] for r in rows}


async def _abandon(link_id: uuid.UUID, requester_node_id: uuid.UUID, lender_node_id: uuid.UUID) -> None:
    """End a link whose create was cancelled, on a session of its own."""
    from coordinator.db.database import get_session_factory

    try:
        async with get_session_factory()() as db:
            ended = await _end_where(db, CircleLink.id == link_id, reason="setup_failed")
            await db.commit()
        await push_removed(ended)
    except Exception:
        logger.exception("circle_link_abandon_failed")


async def _setup_failure(db: AsyncSession, link_id: uuid.UUID) -> None:
    ended = await _end_where(db, CircleLink.id == link_id, reason="setup_failed")
    await db.commit()
    await push_removed(ended)


def _create_response(link: CircleLink, token: str, lender_node: MeshNode, ttl_s: int) -> CircleLinkCreateResponse:
    return CircleLinkCreateResponse(
        link_id=link.id,
        circle_id=link.circle_id,
        model_slug=link.model_slug,
        token=token,
        lender=CircleLinkLender(mesh_ip=str(lender_node.mesh_ip), port=link.lender_port),
        ttl_s=ttl_s,
        renew_after_s=_renew_after(ttl_s),
        expires_at=link.expires_at,
        ready=True,
    )


async def create_link(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    circle_id: uuid.UUID,
    req: CircleLinkCreateRequest,
) -> CircleLinkCreateResponse:
    """POST /me/circles/{circle_id}/links. Validation order: membership, the
    requester machine, its sidecar, the create rate, lender choice, the live
    limit, then setup. Each failure has one code (see link_error calls)."""
    settings = get_settings()
    mesh = _mesh()

    # 1. Membership. Not being in the circle and the circle not existing look the
    # same, as on the relay path, so a circle id cannot be probed.
    if await membership_of(db, circle_id, member_id) is None:
        raise link_error(404, "circle_not_found", "Circle not found.")

    # 2. The requester machine must belong to the caller's own household. A node
    # that exists but is someone else's gets the same answer as one that does not.
    requester_node = await _owned_node(db, req.requester_node_id, member_id)
    if requester_node is None:
        raise link_error(404, "node_not_found", "Machine not found.")

    # 3. Its sidecar must be connected and understand links.
    if not mesh.signal_node_connected(requester_node.id):
        raise link_error(
            409,
            "requester_sidecar_offline",
            "This machine's mesh service is not connected to the network.",
        )
    if LINK_FEATURE not in mesh.signal_node_features(requester_node.id):
        raise link_error(
            409,
            "requester_sidecar_unsupported",
            "This machine's mesh service is too old for direct circle links.",
        )

    # 4. Create rate.
    _count_create_attempt(member_id)

    lock = _create_locks.setdefault(requester_node.id, asyncio.Lock())
    async with lock:
        return await _create_locked(db, settings, member_id, circle_id, req, requester_node)


async def _create_locked(
    db: AsyncSession,
    settings,
    member_id: uuid.UUID,
    circle_id: uuid.UUID,
    req: CircleLinkCreateRequest,
    requester_node: MeshNode,
) -> CircleLinkCreateResponse:
    excluded_peers: set[uuid.UUID] = set()
    excluded_nodes = await _nodes_of_links(db, member_id, req.exclude_link_ids)
    lenders = await lender_member_ids(db, circle_id, member_id)
    ttl_s = settings.circle_link_ttl_s
    direct = settings.circle_link_direct
    refusals = 0  # lenders whose sidecar refused or went away during setup
    last_failure: HTTPException | None = None

    for _ in range(MAX_LENDER_TRIES):
        # 5. Lender: the relay's own selection, plus the link-capable filter.
        peer, _scope = await select_peer_for_request(
            db,
            model_slug=req.model_slug,
            requester_member_id=member_id,
            scope="circle",
            circle_member_ids=lenders,
            exclude_peer_ids=excluded_peers,
            peer_filter=_link_capable(excluded_nodes),
        )
        if peer is None:
            if last_failure is not None:
                raise last_failure
            raise link_error(
                409,
                "no_lender",
                "No machine in this circle can serve this model directly right now.",
            )
        if peer.member_id == member_id:  # cannot happen; the relay rule excludes it
            excluded_peers.add(peer.peer_id)
            continue
        lender_node = await _lender_node(db, peer)
        if lender_node is None or lender_node.id == requester_node.id:
            excluded_peers.add(peer.peer_id)
            continue

        # 6. Live limit. A repeat create for the same pair replaces its row, so
        # that row does not count against the limit.
        now = datetime.now(timezone.utc)
        live = await db.scalar(
            select(func.count())
            .select_from(CircleLink)
            .where(
                CircleLink.requester_member_id == member_id,
                CircleLink.ended_at.is_(None),
                CircleLink.expires_at > now,
                ~(
                    (CircleLink.requester_node_id == requester_node.id)
                    & (CircleLink.lender_node_id == lender_node.id)
                ),
            )
        )
        if int(live or 0) >= MAX_LIVE_LINKS_PER_MEMBER:
            raise link_error(
                429, "too_many_links", "Too many live links. End one or wait for it to expire."
            )

        # 7. Row: supersede the pair's old row (lapsed ones are just expired),
        # then insert.
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(timezone.utc)
        link = CircleLink(
            circle_id=circle_id,
            requester_member_id=member_id,
            requester_node_id=requester_node.id,
            lender_member_id=peer.member_id,
            lender_node_id=lender_node.id,
            lender_peer_id=peer.peer_id,
            lender_port=int(peer.circle_port or 0),
            model_slug=req.model_slug,
            token_hash=hash_token(token),
            created_at=created_at,
            expires_at=(created_at + timedelta(seconds=ttl_s)).replace(microsecond=0),
            renewals=0,
        )
        pair = (
            CircleLink.requester_node_id == requester_node.id,
            CircleLink.lender_node_id == lender_node.id,
        )
        try:
            await _end_where(db, *pair, CircleLink.expires_at <= created_at, reason="expired")
            superseded = await _end_where(db, *pair, reason="superseded")
            db.add(link)
            await db.flush()
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise link_error(409, "link_conflict", "Another link for this pair was just made. Try again.")

        # 8. Tell both sidecars and wait for them.
        try:
            requester_msg = build_add(
                link_id=link.id, role="requester", own=requester_node, other=lender_node,
                port=link.lender_port, ttl_s=ttl_s, token_hash=link.token_hash, direct=direct,
            )
            lender_msg = build_add(
                link_id=link.id, role="lender", own=lender_node, other=requester_node,
                port=link.lender_port, ttl_s=ttl_s, token_hash=link.token_hash, direct=direct,
            )
        except ValidationError:
            # A stored machine record the sidecar could not use (a key that is
            # not 32 bytes of base64, say). Nothing was sent.
            await _setup_failure(db, link.id)
            await push_removed(superseded)
            excluded_peers.add(peer.peer_id)
            last_failure = link_error(
                502, "sidecar_rejected", "A machine's details could not be used for a link.",
                reason="invalid_peer",
            )
            continue

        try:
            results = await _deliver_and_wait(
                link.id,
                [(requester_node.id, requester_msg, "add"), (lender_node.id, lender_msg, "add")],
            )
        except asyncio.CancelledError:
            # The caller went away mid-setup and will never see the token: do not
            # leave a live link nobody can use.
            _spawn(_abandon(link.id, requester_node.id, lender_node.id))
            raise
        req_result, lend_result = results[requester_node.id], results[lender_node.id]

        if req_result.state == "ready" and lend_result.state == "ready":
            await push_removed(superseded)
            over = await _revoked_meanwhile(db, link, requester_node.id, lender_node.id)
            if over is not None:
                raise _gone(over)
            logger.info(
                "circle_link_created",
                link_id=str(link.id),
                circle_id=str(circle_id),
                ttl_s=ttl_s,
                direct=direct,
            )
            return _create_response(link, token, lender_node, ttl_s)

        # Setup failed: revoke, then work out what to tell the caller.
        await _setup_failure(db, link.id)
        await push_removed(superseded)
        logger.info(
            "circle_link_setup_failed",
            link_id=str(link.id),
            requester=req_result.state,
            requester_reason=req_result.reason,
            lender=lend_result.state,
            lender_reason=lend_result.reason,
        )
        if req_result.state == "offline":
            raise link_error(
                409,
                "requester_sidecar_offline",
                "This machine's mesh service is not connected to the network.",
            )
        if req_result.state == "rejected":
            raise link_error(
                502, "sidecar_rejected", "Your machine could not accept the link.",
                reason=req_result.reason,
            )
        if lend_result.state in ("rejected", "offline") and req_result.state == "ready":
            # The lender's machine refused or went away: one more lender.
            refusals += 1
            excluded_peers.add(peer.peer_id)
            last_failure = link_error(
                502,
                "sidecar_rejected",
                "The lender's machine could not accept the link.",
                reason=lend_result.reason or "tunnel_error",
            )
            if refusals >= 2:
                raise last_failure
            continue
        raise link_error(
            504, "sidecar_timeout", "A machine did not confirm the link in time. Try again."
        )

    if last_failure is not None:
        raise last_failure
    raise link_error(409, "no_lender", "No machine in this circle can serve this model directly right now.")


# ---------------------------------------------------------------------------
# Renew and end
# ---------------------------------------------------------------------------


async def _requesters_link(
    db: AsyncSession, member_id: uuid.UUID, circle_id: uuid.UUID, link_id: uuid.UUID
) -> CircleLink | None:
    return (
        await db.execute(
            select(CircleLink).where(
                CircleLink.id == link_id,
                CircleLink.circle_id == circle_id,
                CircleLink.requester_member_id == member_id,
            )
        )
    ).scalar_one_or_none()


def _gone(reason: str | None) -> HTTPException:
    return link_error(
        410, "link_ended", "This link has ended. Make a new one.", reason=reason or "ended"
    )


async def renew_link(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    circle_id: uuid.UUID,
    link_id: uuid.UUID,
) -> CircleLinkRenewResponse:
    """POST /me/circles/{circle_id}/links/{link_id}/renew.

    404 unless the link is the caller's. 410 once it has ended, whatever the
    reason; a renewal also re-checks that the circle still allows it, ending the
    link (and answering 410) if not. Sidecar trouble never fails a renewal: the
    answer says which sidecars acknowledged."""
    settings = get_settings()
    link = await _requesters_link(db, member_id, circle_id, link_id)
    if link is None:
        raise link_error(404, "link_not_found", "Link not found.")
    if link.ended_at is not None:
        raise _gone(link.end_reason)

    now = datetime.now(timezone.utc)
    why: str | None = None
    if link.expires_at <= now:
        why = "expired"
    elif await membership_of(db, circle_id, member_id) is None:
        why = "member_left"
    elif link.lender_member_id not in await lender_member_ids(db, circle_id, member_id):
        why = "member_removed"
    else:
        lender = await get_peer(link.lender_peer_id) if link.lender_peer_id else None
        if lender is None:
            if link.lender_peer_id is None or not _lender_may_return(link.lender_peer_id):
                why = "lender_gone"
        elif not lender.serves_circle:
            why = "sharing_off"
        elif lender.mesh_node_id != link.lender_node_id:
            why = "lender_gone"
    if why is not None:
        ended = await _end_where(db, CircleLink.id == link.id, reason=why)
        await db.commit()
        await push_removed(ended)
        raise _gone(why)

    ttl_s = settings.circle_link_ttl_s
    direct = settings.circle_link_direct
    # Not faster than the link needs: each renewal messages the lender's sidecar.
    gap = max(1, ttl_s // RENEW_MIN_GAP_DIVISOR)
    last = link.renewed_at or link.created_at
    since = (now - last).total_seconds()
    if since < gap:
        raise link_error(
            429,
            "too_many_links",
            "This link was renewed a moment ago. Wait and try again.",
            headers={"Retry-After": str(max(1, int(gap - since) + 1))},
        )
    link.expires_at = (now + timedelta(seconds=ttl_s)).replace(microsecond=0)
    link.renewed_at = now
    link.renewals = link.renewals + 1
    await db.commit()

    requester_node = await db.get(MeshNode, link.requester_node_id)
    lender_node = await db.get(MeshNode, link.lender_node_id)
    delivered = {"requester": False, "lender": False}
    if requester_node is not None and lender_node is not None:
        try:
            sends = [
                (requester_node.id,
                 build_extend(link_id=link.id, own=requester_node, other=lender_node,
                              ttl_s=ttl_s, direct=direct), "extend"),
                (lender_node.id,
                 build_extend(link_id=link.id, own=lender_node, other=requester_node,
                              ttl_s=ttl_s, direct=direct), "extend"),
            ]
            results = await _deliver_and_wait(link.id, sends)
            # A sidecar that does not know the link (it restarted) says so; send
            # the add again to that one.
            resend = []
            for node, role, own, other in (
                (requester_node.id, "requester", requester_node, lender_node),
                (lender_node.id, "lender", lender_node, requester_node),
            ):
                if results[node].state == "rejected" and results[node].reason == "unknown_grant":
                    resend.append((node, build_add(
                        link_id=link.id, role=role, own=own, other=other, port=link.lender_port,
                        ttl_s=ttl_s, token_hash=link.token_hash, direct=direct), "add"))
            if resend:
                results.update(await _deliver_and_wait(link.id, resend))
                # An add sent after a revocation would bring the link back to
                # life on that sidecar; if the link ended meanwhile, undo it.
                over = await _revoked_meanwhile(db, link, requester_node.id, lender_node.id)
                if over is not None:
                    raise _gone(over)
            delivered = {
                "requester": results[requester_node.id].state == "ready",
                "lender": results[lender_node.id].state == "ready",
            }
        except HTTPException:
            raise
        except Exception:
            logger.exception("circle_link_renew_delivery_failed", link_id=str(link.id))
    logger.info("circle_link_renewed", link_id=str(link.id), renewals=link.renewals)
    return CircleLinkRenewResponse(
        link_id=link.id,
        ttl_s=ttl_s,
        renew_after_s=_renew_after(ttl_s),
        expires_at=link.expires_at,
        delivered=CircleLinkDelivered(**delivered),
    )


async def end_link(
    db: AsyncSession, *, member_id: uuid.UUID, circle_id: uuid.UUID, link_id: uuid.UUID
) -> None:
    """DELETE /me/circles/{circle_id}/links/{link_id}. Idempotent: ending a link
    that is already over, or that is not the caller's, changes nothing and says
    nothing about whether it exists."""
    ended = await _end_where(
        db,
        CircleLink.id == link_id,
        CircleLink.circle_id == circle_id,
        CircleLink.requester_member_id == member_id,
        reason="ended",
    )
    await db.commit()
    await push_removed(ended)


# ---------------------------------------------------------------------------
# Replay on sidecar reconnect
# ---------------------------------------------------------------------------


async def replay_for_node(node_id: uuid.UUID) -> None:
    """Bring a reconnecting sidecar up to date with its links.

    A sidecar that restarted has lost its links, and one whose signal channel was
    down missed any removal sent meanwhile; a signal reconnect (every coordinator
    restart, too) is the moment to put both right. So it is sent, first, a remove
    for each link that ended in the last REPLAY_REMOVE_WINDOW (a sidecar that does
    not hold the link ignores it), and then an add for each live link, carrying
    its remaining lifetime so a link never outlives its row. Removes go first so
    one can never undo a later add. Nothing is waited for; a sidecar that already
    holds a link acknowledges the add again."""
    if not links_enabled():
        return
    mesh = _mesh()
    if LINK_FEATURE not in mesh.signal_node_features(node_id):
        return
    from coordinator.db.database import get_session_factory

    direct = get_settings().circle_link_direct
    now = datetime.now(timezone.utc)
    mine = (CircleLink.requester_node_id == node_id) | (CircleLink.lender_node_id == node_id)
    async with get_session_factory()() as db:
        recently_ended = (
            await db.execute(
                select(CircleLink.id, CircleLink.end_reason)
                .where(CircleLink.ended_at > now - REPLAY_REMOVE_WINDOW, mine)
                .order_by(CircleLink.ended_at.desc())
                .limit(100)
            )
        ).all()
        for link_id, end_reason in recently_ended:
            await _send(node_id, build_remove(link_id, end_reason))
        links = (
            await db.execute(
                select(CircleLink).where(
                    CircleLink.ended_at.is_(None), CircleLink.expires_at > now, mine
                )
            )
        ).scalars().all()
        for link in links:
            ttl_s = _remaining_ttl_s(link, now)
            if ttl_s < 5:
                continue  # about to lapse; the sweeper will end it
            requester_node = await db.get(MeshNode, link.requester_node_id)
            lender_node = await db.get(MeshNode, link.lender_node_id)
            if requester_node is None or lender_node is None:
                continue
            is_lender = link.lender_node_id == node_id
            try:
                payload = build_add(
                    link_id=link.id,
                    role="lender" if is_lender else "requester",
                    own=lender_node if is_lender else requester_node,
                    other=requester_node if is_lender else lender_node,
                    port=link.lender_port,
                    ttl_s=min(ttl_s, 1800),
                    token_hash=link.token_hash,
                    direct=direct,
                )
            except ValidationError:
                continue
            if await _send(node_id, payload):
                # Ended while this add was going out: take it back (see
                # _revoked_meanwhile).
                await _revoked_meanwhile(db, link, requester_node.id, lender_node.id)


# ---------------------------------------------------------------------------
# Lender engine events (called from routers/peers.py)
# ---------------------------------------------------------------------------

# peer_id -> the task that ends that lender's links if it does not come back.
_grace_tasks: dict[uuid.UUID, asyncio.Task] = {}


def _lender_may_return(peer_id: uuid.UUID) -> bool:
    """A lender engine that is offline right now still holds its links while its
    disconnect grace runs, and for the first seconds after this process starts
    (every engine reconnects after a coordinator restart, and no grace timer
    survives one)."""
    return (
        peer_id in _grace_tasks
        or time.monotonic() - _PROCESS_STARTED <= LENDER_DISCONNECT_GRACE_S
    )


async def _end_lender_links(peer_id: uuid.UUID, reason: str, *conditions) -> None:
    from coordinator.db.database import get_session_factory

    async with get_session_factory()() as db:
        ended = await _end_where(db, CircleLink.lender_peer_id == peer_id, *conditions, reason=reason)
        await db.commit()
    await push_removed(ended)


async def on_peer_registered(
    peer_id: uuid.UUID,
    *,
    serves_circle: bool,
    mesh_node_id: uuid.UUID | None,
    circle_port: int | None,
) -> None:
    """A lender engine (re)connected. Cancels any disconnect grace, then ends the
    links it can no longer back: all of them if it no longer shares with
    circles, and those on a machine or port it no longer answers on. Never
    raises."""
    try:
        task = _grace_tasks.pop(peer_id, None)
        if task is not None:
            task.cancel()
        if not links_enabled():
            return
        if not serves_circle:
            await _end_lender_links(peer_id, "sharing_off")
        elif mesh_node_id is None or circle_port is None:
            await _end_lender_links(peer_id, "lender_gone")
        else:
            await _end_lender_links(
                peer_id,
                "lender_gone",
                (CircleLink.lender_node_id != mesh_node_id) | (CircleLink.lender_port != circle_port),
            )
    except Exception:
        logger.exception("circle_link_peer_registered_hook_failed")


async def _grace_then_end(peer_id: uuid.UUID) -> None:
    try:
        await asyncio.sleep(LENDER_DISCONNECT_GRACE_S)
        if await get_peer(peer_id) is not None:
            return  # back in time
        await _end_lender_links(peer_id, "lender_gone")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("circle_link_grace_failed")
    finally:
        if _grace_tasks.get(peer_id) is asyncio.current_task():
            _grace_tasks.pop(peer_id, None)


def on_peer_gone(peer_id: uuid.UUID, mesh_node_id: uuid.UUID | None) -> None:
    """A lender engine's connection ended. Its links end after the grace unless
    it reconnects first. Not for engines with no mesh binding (they hold no
    links). Never raises."""
    try:
        if mesh_node_id is None or not links_enabled():
            return
        old = _grace_tasks.pop(peer_id, None)
        if old is not None:
            old.cancel()
        _grace_tasks[peer_id] = asyncio.create_task(_grace_then_end(peer_id))
    except Exception:
        logger.exception("circle_link_peer_gone_hook_failed")


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    expired: int = 0
    reconciled: int = 0
    deleted: int = 0


async def _reconcile(db: AsyncSession) -> list[EndedLink]:
    """End live links the circle or the lender no longer backs. The revocation
    hooks do this at the moment of the change; this catches what a failed hook,
    a lost grace timer after a restart, or an edit made outside the routes left
    behind."""
    ended: list[EndedLink] = []
    circles = (
        await db.execute(
            select(CircleLink.circle_id).where(CircleLink.ended_at.is_(None)).distinct()
        )
    ).all()
    for (circle_id,) in circles:
        ended += await end_not_allowed_in_circle(db, circle_id, "member_removed")

    rows = (
        await db.execute(
            select(CircleLink.id, CircleLink.lender_peer_id, CircleLink.lender_node_id).where(
                CircleLink.ended_at.is_(None)
            )
        )
    ).all()
    doomed: dict[str, list[uuid.UUID]] = {}
    for link_id, peer_id, node_id in rows:
        if peer_id is None:
            doomed.setdefault("lender_gone", []).append(link_id)
            continue
        conn = await get_peer(peer_id)
        if conn is None:
            if not _lender_may_return(peer_id):
                doomed.setdefault("lender_gone", []).append(link_id)
        elif not conn.serves_circle:
            doomed.setdefault("sharing_off", []).append(link_id)
        elif conn.mesh_node_id != node_id:
            doomed.setdefault("lender_gone", []).append(link_id)
    for why, ids in doomed.items():
        ended += await _end_where(db, CircleLink.id.in_(ids), reason=why)
    return ended


async def sweep_once(session_factory: async_sessionmaker) -> SweepResult:
    """One pass: end lapsed links (and tell their sidecars), reconcile against
    the circles and lenders, delete rows that ended over RETENTION ago, and
    drop idle in-memory counters."""
    result = SweepResult()
    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        expired = await _end_where(db, CircleLink.expires_at <= now, reason="expired")
        reconciled = await _reconcile(db)
        deleted = await db.execute(
            delete(CircleLink).where(CircleLink.ended_at < now - RETENTION)
        )
        await db.commit()
    result.expired, result.reconciled = len(expired), len(reconciled)
    result.deleted = deleted.rowcount or 0
    await push_removed(expired + reconciled)

    mono = time.monotonic()
    for member_id in [m for m, q in _create_attempts.items() if not q or mono - q[-1] > 60.0]:
        _create_attempts.pop(member_id, None)
    for node_id in [n for n, lock in _create_locks.items() if not lock.locked()]:
        _create_locks.pop(node_id, None)
    return result


async def run_sweeper_loop(stop: asyncio.Event, interval_s: float | None = None) -> None:
    """Background loop wired into the coordinator lifespan (same shape as
    services/peer_reaper.py: interruptible sleep, a failed pass is logged and the
    loop carries on)."""
    from coordinator.db.database import get_session_factory

    interval = float(interval_s if interval_s is not None else get_settings().circle_link_sweep_interval_s)
    logger.info("circle_link_sweeper_started", interval_s=interval)
    while not stop.is_set():
        try:
            swept = await sweep_once(get_session_factory())
            if swept.expired or swept.reconciled or swept.deleted:
                logger.info(
                    "circle_link_sweep",
                    expired=swept.expired,
                    reconciled=swept.reconciled,
                    deleted=swept.deleted,
                )
        except Exception:
            logger.exception("circle_link_sweeper_iteration_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("circle_link_sweeper_stopped")
