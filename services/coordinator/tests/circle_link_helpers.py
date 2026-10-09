"""Shared scaffolding for the direct circle link tests.

A World is a requester and any number of lenders in different households, all
in one circle, with real rows in Postgres, the lenders' engines registered in
the peer registry, and a FakeSidecar standing in for each machine's mesh
sidecar on /mesh/signal. The FakeSidecar records everything the coordinator
sends it and answers circle_peer_add / _extend the way a configured sidecar
would, so the real create, renew and revocation code runs end to end except for
the websocket transport (tests/test_circle_link_wire.py covers that).
"""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from coordinator.models import (
    CircleLink,
    CircleMember,
    CircleMembership,
    Household,
    MemberKey,
    MeshNode,
    Peer,
    PeerCapability,
    TrustedCircle,
)
from coordinator.middleware.node_auth import hash_node_token
from coordinator.routers import mesh
from coordinator.services import circle_links
from coordinator.services.peer_registry import PeerConnection, register_peer, unregister_peer


class _WS:  # the engine websocket: registry code stores it, never calls it
    pass


class FakeSidecar:
    """One machine's mesh sidecar on /mesh/signal.

    `behaviour` decides how it answers an add or extend, by op:
      "ready"            acknowledge ready
      ("reject", reason) acknowledge rejected with that reason
      "silent"           never answer
    A dict maps op to one of those (default "ready" for ops it omits).
    """

    def __init__(self, node_id: uuid.UUID, behaviour: Any = "ready") -> None:
        self.node_id = node_id
        self.behaviour = behaviour
        self.sent: list[dict] = []
        self._tasks: set[asyncio.Task] = set()
        self.closed = False
        # A sidecar that has stopped reading its socket: a send never completes.
        self.hang = False
        # Called with each payload as it is sent, before any ack. Lets a test
        # change the world at an exact moment.
        self.on_send: Callable[[dict], None] | None = None

    def _answer(self, op: str) -> Any:
        if isinstance(self.behaviour, dict):
            return self.behaviour.get(op, "ready")
        return self.behaviour

    async def send_json(self, payload: dict) -> None:
        if self.hang:
            await asyncio.sleep(3600)
        self.sent.append(payload)
        if self.on_send is not None:
            self.on_send(payload)
        kind = payload.get("type")
        if kind not in ("circle_peer_add", "circle_peer_extend"):
            return
        op = "add" if kind == "circle_peer_add" else "extend"
        answer = self._answer(op)
        if answer == "silent":
            return
        ack = {"type": "circle_peer_ack", "link_id": payload["link_id"], "op": op,
               "state": "ready", "reason": None}
        if isinstance(answer, tuple) and answer[0] == "reject":
            ack.update(state="rejected", reason=answer[1])
        task = asyncio.get_running_loop().create_task(circle_links.handle_ack(self.node_id, ack))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


@dataclass
class Machine:
    member_id: uuid.UUID
    household_id: uuid.UUID
    node_id: uuid.UUID
    node_name: str
    mesh_ip: str
    wg_key: str
    sidecar: FakeSidecar
    peer_id: uuid.UUID | None = None
    hostname: str | None = None
    public_key: str | None = None
    circle_port: int | None = None


def wg_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def report_endpoints(machine: "Machine", external: str, local: Iterable[str] = ()) -> None:
    """Make the machine's live connection (its FakeSidecar) the source of an
    endpoint report, as the endpoint_update handler does for a token-bound
    sidecar. The values are stored as given, unfiltered, so a test can show that
    the broker filters LAN candidates again itself. The direct policy takes a
    machine's addresses from this report and from nothing else (see
    routers.mesh.signal_link_endpoints)."""
    mesh._endpoint_reports[machine.node_id] = mesh._EndpointReport(
        ws=machine.sidecar,  # type: ignore[arg-type]
        external_endpoint=external,
        local_endpoints=tuple(local),
    )


async def issue_node_token(factory: async_sessionmaker, node_id: uuid.UUID) -> dict[str, str]:
    """Give a node row a node_token the way /mesh/enroll does (a fresh 256-bit
    urlsafe token, only its hash stored in mesh_nodes.node_token_hash) and return
    the header a sidecar sends with it on the /mesh/signal upgrade:
    {"Authorization": "Bearer <token>"}. The coordinator checks that header against
    the row, so a connection made with it is the node's own, token-bound one."""
    raw = secrets.token_urlsafe(32)
    async with factory() as db:
        async with db.begin():
            await db.execute(
                update(MeshNode).where(MeshNode.id == node_id).values(node_token_hash=hash_node_token(raw))
            )
    return {"Authorization": f"Bearer {raw}"}


async def fresh_address(db, taken: set[tuple[int, int]] | None = None) -> tuple[str, str]:
    """A /24 of the shared mesh pool that no household has, and a host in it.

    Drawn from the top of 100.64.0.0/10, far from where real households are
    allocated (lowest free first), and checked against the table, so a leftover
    row in a shared development database cannot make a test fail."""
    while True:
        second, third = 112 + secrets.randbelow(16), secrets.randbelow(256)
        if taken is not None and (second, third) in taken:
            continue
        cidr = f"100.{second}.{third}.0/24"
        if (await db.execute(text("SELECT 1 FROM households WHERE mesh_cidr = :c"), {"c": cidr})).first() is None:
            if taken is not None:
                taken.add((second, third))
            return cidr, f"100.{second}.{third}.2"


@dataclass
class World:
    factory: async_sessionmaker
    circle_id: uuid.UUID
    circle_name: str
    model: str
    owner_id: uuid.UUID
    requester: Machine
    lenders: list[Machine] = field(default_factory=list)
    others: list[Machine] = field(default_factory=list)
    _taken: set = field(default_factory=set)
    _extra_members: list[uuid.UUID] = field(default_factory=list)

    @property
    def lender(self) -> Machine:
        return self.lenders[0]

    @property
    def member_id(self) -> uuid.UUID:
        return self.requester.member_id

    async def add_lender(
        self,
        *,
        in_circle: bool = True,
        via: str = "membership",
        serves_circle: bool = True,
        serves_pool: bool = True,
        feature: bool = True,
        connected: bool = True,
        bound: bool = True,
        circle_port: int | None = 8347,
        tokens_per_second: float | None = None,
        sidecar: Any = "ready",
        model: str | None = None,
    ) -> Machine:
        """Another household with one machine and one engine offering the model."""
        member = uuid.uuid4()
        tag = uuid.uuid4().hex[:8]
        key = wg_key()
        public_key = f"lend-key-{tag}"
        async with self.factory() as db:
            async with db.begin():
                cidr, ip = await fresh_address(db, self._taken)
                household = Household(owner_member_id=member, name=f"hh-{tag}", mesh_cidr=cidr)
                db.add(household)
                await db.flush()
                node = MeshNode(household_id=household.id, node_name=f"lender-box-{tag}",
                                mesh_ip=ip, wg_public_key=key, platform="linux")
                db.add(node)
                peer = Peer(member_id=member, public_key=public_key, hostname=f"lender-host-{tag}",
                            hardware_summary={}, status="active")
                db.add(peer)
                await db.flush()
                db.add(PeerCapability(peer_id=peer.id, model_slug=model or self.model,
                                      quantization="q4", tokens_per_second=tokens_per_second))
                db.add(MemberKey(public_key=public_key, member_id=member, added_via="seed"))
                if in_circle and via == "membership":
                    db.add(CircleMembership(circle_id=self.circle_id, member_id=member, role="member"))
                elif in_circle and via == "legacy_key":
                    db.add(CircleMember(circle_id=self.circle_id, member_pubkey=public_key))
        machine = Machine(member_id=member, household_id=household.id, node_id=node.id,
                          node_name=node.node_name, mesh_ip=ip, wg_key=key,
                          sidecar=FakeSidecar(node.id, sidecar), peer_id=peer.id,
                          hostname=f"lender-host-{tag}", public_key=public_key,
                          circle_port=circle_port)
        if connected:
            mesh._signal_connections[node.id] = machine.sidecar  # type: ignore[assignment]
            mesh._signal_features[node.id] = frozenset({circle_links.LINK_FEATURE} if feature else ())
        await register_peer(PeerConnection(
            peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(), member_id=member,
            serves_circle=serves_circle, serves_pool=serves_pool,
            mesh_node_id=node.id if bound else None,
            circle_port=circle_port if bound else None,
        ))
        self.lenders.append(machine)
        return machine

    async def add_requester(self) -> Machine:
        """A second circle member with a machine of their own, connected to the
        mesh and able to ask for links."""
        member = uuid.uuid4()
        tag = uuid.uuid4().hex[:8]
        key = wg_key()
        async with self.factory() as db:
            async with db.begin():
                cidr, ip = await fresh_address(db, self._taken)
                household = Household(owner_member_id=member, name=f"hh-x-{tag}", mesh_cidr=cidr)
                db.add(household)
                await db.flush()
                node = MeshNode(household_id=household.id, node_name=f"x-laptop-{tag}",
                                mesh_ip=ip, wg_public_key=key, platform="darwin")
                db.add(node)
                db.add(CircleMembership(circle_id=self.circle_id, member_id=member, role="member"))
        machine = Machine(member_id=member, household_id=household.id, node_id=node.id,
                          node_name=node.node_name, mesh_ip=ip, wg_key=key,
                          sidecar=FakeSidecar(node.id))
        mesh._signal_connections[node.id] = machine.sidecar  # type: ignore[assignment]
        mesh._signal_features[node.id] = frozenset({circle_links.LINK_FEATURE})
        self.others.append(machine)
        return machine

    async def add_member(self, *, role: str = "member") -> uuid.UUID:
        """A circle member with no machines."""
        member = uuid.uuid4()
        async with self.factory() as db:
            async with db.begin():
                db.add(CircleMembership(circle_id=self.circle_id, member_id=member, role=role))
        self._extra_members.append(member)
        return member

    # ---- reads -----------------------------------------------------------------

    async def row(self, link_id: uuid.UUID | str) -> CircleLink | None:
        async with self.factory() as db:
            return await db.get(CircleLink, uuid.UUID(str(link_id)))

    async def rows(self) -> list[CircleLink]:
        async with self.factory() as db:
            return list((await db.execute(
                select(CircleLink).where(CircleLink.circle_id == self.circle_id)
                .order_by(CircleLink.created_at)
            )).scalars().all())

    async def live(self) -> list[CircleLink]:
        return [r for r in await self.rows() if r.ended_at is None]

    # ---- teardown --------------------------------------------------------------

    async def close(self) -> None:
        machines = [self.requester, *self.lenders, *self.others]
        for m in machines:
            mesh._signal_connections.pop(m.node_id, None)
            mesh._signal_features.pop(m.node_id, None)
            mesh._local_endpoints.pop(m.node_id, None)
            mesh._endpoint_reports.pop(m.node_id, None)
            if m.peer_id is not None:
                await unregister_peer(m.peer_id)
        for task in list(circle_links._grace_tasks.values()):
            task.cancel()
        circle_links._grace_tasks.clear()
        async with self.factory() as db:
            async with db.begin():
                await db.execute(delete(TrustedCircle).where(TrustedCircle.id == self.circle_id))
                await db.execute(delete(Household).where(
                    Household.id.in_([m.household_id for m in machines])))
                peer_ids = [m.peer_id for m in machines if m.peer_id]
                if peer_ids:
                    await db.execute(delete(Peer).where(Peer.id.in_(peer_ids)))
                keys = [m.public_key for m in machines if m.public_key]
                if keys:
                    await db.execute(delete(MemberKey).where(MemberKey.public_key.in_(keys)))


async def build_world(
    factory: async_sessionmaker,
    *,
    lenders: int = 1,
    requester_owns_circle: bool = True,
    requester_sidecar: Any = "ready",
    requester_feature: bool = True,
    requester_connected: bool = True,
    lender_kwargs: dict | None = None,
) -> World:
    """A requester machine and `lenders` lender machines, all in one circle.

    By default the requester owns the circle. With requester_owns_circle False a
    third member owns it and the requester joins as an ordinary member, which
    is the case where the requester itself can leave."""
    tag = uuid.uuid4().hex[:8]
    taken: set = set()
    member = uuid.uuid4()
    owner = member if requester_owns_circle else uuid.uuid4()
    key = wg_key()
    model = f"link-model-{tag}"
    async with factory() as db:
        async with db.begin():
            cidr, ip = await fresh_address(db, taken)
            household = Household(owner_member_id=member, name=f"hh-req-{tag}", mesh_cidr=cidr)
            db.add(household)
            await db.flush()
            node = MeshNode(household_id=household.id, node_name=f"req-laptop-{tag}",
                            mesh_ip=ip, wg_public_key=key, platform="darwin")
            circle = TrustedCircle(owner_member_id=owner, name=f"Circle-{tag}")
            db.add_all([node, circle])
            await db.flush()
            db.add(CircleMembership(circle_id=circle.id, member_id=owner, role="owner"))
            if not requester_owns_circle:
                db.add(CircleMembership(circle_id=circle.id, member_id=member, role="member"))
    requester = Machine(member_id=member, household_id=household.id, node_id=node.id,
                        node_name=node.node_name, mesh_ip=ip, wg_key=key,
                        sidecar=FakeSidecar(node.id, requester_sidecar))
    if requester_connected:
        mesh._signal_connections[node.id] = requester.sidecar  # type: ignore[assignment]
        mesh._signal_features[node.id] = frozenset(
            {circle_links.LINK_FEATURE} if requester_feature else ()
        )
    world = World(factory=factory, circle_id=circle.id, circle_name=circle.name, model=model,
                  owner_id=owner, requester=requester, _taken=taken)
    for _ in range(lenders):
        await world.add_lender(**(lender_kwargs or {}))
    return world


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck")


async def raw_dump(factory: async_sessionmaker, link_id: uuid.UUID) -> str:
    """Every column of the link's row as one string, to prove what is stored."""
    async with factory() as db:
        row = (await db.execute(
            text("SELECT t::text FROM circle_links t WHERE id = :id"), {"id": link_id}
        )).scalar_one()
    return row
