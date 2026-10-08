"""Trusted-circle membership: who is in a circle, who may borrow from it, and
whose machines lend to a given request.

Two kinds of entry put someone in a circle:

* a ``CircleMembership``: the owner from creation, anyone else by accepting
  an invite. Members borrow from each other and can leave.
* a legacy ``CircleMember`` key the owner pasted in with an older app. The
  member behind that key lends, as before, but holds no membership.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.models.circle import CircleMember, CircleMembership
from coordinator.models.member_key import MemberKey

# No 0/O, 1/I/L or U: a code is read aloud and typed by hand.
INVITE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
INVITE_CODE_LENGTH = 12
MAX_CIRCLE_PEOPLE = 50

_CODE_IN_LINK = re.compile(r"[?&]code=([^&#\s]+)")


def new_invite_code() -> str:
    """A fresh code, grouped for reading: ``ABCD-EFGH-JKMN``. About 58 bits."""
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, INVITE_CODE_LENGTH, 4))


def normalise_invite_code(raw: str) -> str | None:
    """The canonical form of whatever the person pasted: the code with or
    without dashes and spaces, in any case, or a whole invite link. None when
    it cannot be a code."""
    value = (raw or "").strip()
    link = _CODE_IN_LINK.search(value)
    if link:
        value = link.group(1)
    value = re.sub(r"[\s-]", "", value).upper()
    if len(value) != INVITE_CODE_LENGTH or any(c not in INVITE_ALPHABET for c in value):
        return None
    return value


def hash_invite_code(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def membership_of(
    db: AsyncSession, circle_id: uuid.UUID, member_id: uuid.UUID
) -> CircleMembership | None:
    return (await db.execute(
        select(CircleMembership).where(
            CircleMembership.circle_id == circle_id,
            CircleMembership.member_id == member_id,
        )
    )).scalar_one_or_none()


async def lender_member_ids(
    db: AsyncSession, circle_id: uuid.UUID, requester: uuid.UUID
) -> set[uuid.UUID]:
    """The members whose machines may answer ``requester``'s request to this
    circle: everyone in it except the requester. A circle request is for
    borrowing; the requester's own machines are the My Machines scope."""
    ids = {
        row[0] for row in (await db.execute(
            select(CircleMembership.member_id).where(CircleMembership.circle_id == circle_id)
        )).all()
    }
    legacy_keys = {
        row[0] for row in (await db.execute(
            select(CircleMember.member_pubkey).where(CircleMember.circle_id == circle_id)
        )).all()
    }
    if legacy_keys:
        ids |= {
            row[0] for row in (await db.execute(
                select(MemberKey.member_id).where(MemberKey.public_key.in_(legacy_keys))
            )).all()
        }
    ids.discard(requester)
    return ids


async def member_names(db: AsyncSession, member_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """What to call each member inside a circle they share: the name they
    set, else their sign-in email. Read from the account service's table;
    members with no row get no entry."""
    if not member_ids:
        return {}
    rows = (await db.execute(
        text("SELECT id, display_name, email FROM members WHERE id = ANY(:ids)"),
        {"ids": list(member_ids)},
    )).all()
    out: dict[uuid.UUID, str] = {}
    for member_id, display_name, email in rows:
        name = (display_name or "").strip() or (email or "").strip()
        if name:
            out[member_id if isinstance(member_id, uuid.UUID) else uuid.UUID(str(member_id))] = name
    return out
