"""Household CIDR allocation.

Carves a unique /24 out of the global mesh pool for each household.
Pool is `100.64.0.0/10` - the Carrier-Grade NAT range - which gives us
~4M possible /24 slices and guaranteed non-collision with home/corp
networks (10.0.0.0/8 and 192.168.0.0/16 are common; 100.64.0.0/10 is
not). Matches the convention used by Tailscale + Headscale.

Allocation strategy: sequential lowest-unused /24. Races at scale are
handled by the UNIQUE constraint on `households.mesh_cidr`.
"""
from __future__ import annotations

import ipaddress

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.household import Household

# The global pool we carve households out of.
HOUSEHOLD_POOL = ipaddress.ip_network("100.64.0.0/10")
HOUSEHOLD_PREFIX_LEN = 24  # each household gets a /24


class HouseholdCIDRExhausted(Exception):
    """No /24 left in the global pool. Practically impossible at v0
    scale (~4M households fit); surfaced as 503 if it ever happens."""


async def allocate_household_cidr(db: AsyncSession) -> str:
    """Return the lowest-numbered /24 in `HOUSEHOLD_POOL` not already
    in use by an existing household."""
    result = await db.execute(select(Household.mesh_cidr))
    used = {str(row[0]) for row in result.all()}

    for subnet in HOUSEHOLD_POOL.subnets(new_prefix=HOUSEHOLD_PREFIX_LEN):
        candidate = str(subnet)
        if candidate not in used:
            return candidate

    raise HouseholdCIDRExhausted(
        f"global pool {HOUSEHOLD_POOL} exhausted at /{HOUSEHOLD_PREFIX_LEN}"
    )
