"""Mesh-IP allocation logic.

A household is allocated a contiguous IP range (mesh_cidr). Each node
in the household gets one IP from that range. v0 uses a sequential
allocator that picks the lowest unused address — collisions impossible
because the column has a UNIQUE constraint, and the household's range
is large enough (a /24 = 254 usable hosts) that we'll never exhaust it
for any real household.

The allocator is process-local but the underlying state is the database
— each allocation reads `mesh_nodes WHERE household_id = ?` and picks
the next available address. Concurrent enrollments could race; the
UNIQUE constraint serializes them at the DB layer.
"""
from __future__ import annotations

import ipaddress
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.mesh_node import MeshNode


class MeshIPExhausted(Exception):
    """Raised when a household's CIDR has no free addresses left.

    Practically impossible with a /24 + sane household sizes; surfaced
    as a 503 to the enrollment caller so they know to contact support.
    """


async def allocate_mesh_ip(db: AsyncSession, household_id: uuid.UUID, mesh_cidr: str) -> str:
    """Pick the lowest unused host address in the household's CIDR.

    Skips:
      * the network address (.0)
      * the broadcast address (.255 on a /24)
      * any address already assigned to an existing mesh_node row
    """
    network = ipaddress.ip_network(mesh_cidr, strict=False)

    # Pull existing IPs as a set for O(1) membership tests.
    result = await db.execute(
        select(MeshNode.mesh_ip).where(MeshNode.household_id == household_id)
    )
    used = {row[0] for row in result.all()}

    for host in network.hosts():
        candidate = str(host)
        if candidate not in used:
            return candidate

    raise MeshIPExhausted(
        f"household {household_id} mesh_cidr {mesh_cidr} has no free addresses"
    )
