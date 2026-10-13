"""Joining a trusted circle by invite, and what membership then allows.

A circle starts as a list of machine keys its owner pasted in. The person
behind a key is never asked, cannot see the circle or leave it, and cannot
borrow from it. An invite is the consent step; a membership is what both
sides hold afterwards.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text, update

from coordinator.models.circle import CircleInvite, CircleMember, CircleMembership
from coordinator.models.member_key import MemberKey
from coordinator.models.peer import Peer, PeerCapability
from coordinator.routers import circles as circles_router
from coordinator.routers.circles import (
    AcceptInviteRequest,
    CreateCircleRequest,
    CreateInviteRequest,
    accept_invite,
    circle_availability,
    create_circle,
    create_invite,
    delete_circle,
    leave_circle,
    list_circles,
    list_invites,
    preview_invite,
    remove_person,
    revoke_invite,
)
from coordinator.services.circles import (
    INVITE_ALPHABET,
    hash_invite_code,
    lender_member_ids,
    new_invite_code,
    normalise_invite_code,
)
from coordinator.services.peer_registry import (
    PeerConnection,
    register_peer,
    select_peer_for_request,
    unregister_peer,
)
from tests.test_circles import _WS


class Sessions:
    """Runs each endpoint call in its own session, the way a request does, so
    a commit inside one call is visible to the next. Deletes what the test
    created afterwards."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.circles: list[uuid.UUID] = []
        self.members: list[uuid.UUID] = []
        self.keys: list[str] = []

    async def call(self, fn, *args, member: uuid.UUID, **kw):
        async with self.factory() as db:
            return await fn(*args, db=db, member_id=member, **kw)

    async def circle(self, owner: uuid.UUID, name: str = "Friends"):
        out = await self.call(create_circle, CreateCircleRequest(name=name), member=owner)
        self.circles.append(out.id)
        return out

    async def invite(self, circle_id, owner, **kw) -> str:
        return (await self.call(create_invite, circle_id, CreateInviteRequest(**kw), member=owner)).code

    async def join(self, code: str, member: uuid.UUID):
        return await self.call(accept_invite, AcceptInviteRequest(code=code), member=member)

    async def listed(self, member: uuid.UUID):
        return (await self.call(list_circles, member=member)).circles

    async def status(self, fn, *args, member: uuid.UUID) -> int:
        with pytest.raises(HTTPException) as exc:
            await self.call(fn, *args, member=member)
        return exc.value.status_code

    async def cleanup(self) -> None:
        async with self.factory() as db:
            if self.circles:
                await db.execute(text("DELETE FROM trusted_circles WHERE id = ANY(:ids)"), {"ids": self.circles})
            if self.keys:
                await db.execute(text("DELETE FROM member_keys WHERE public_key = ANY(:keys)"), {"keys": self.keys})
            if self.members:
                await db.execute(text("DELETE FROM members WHERE id = ANY(:ids)"), {"ids": self.members})
            await db.commit()


@pytest_asyncio.fixture()
async def sessions(session_factory):
    s = Sessions(session_factory.factory)
    try:
        yield s
    finally:
        await s.cleanup()


# ---- the code itself ---------------------------------------------------------


def test_a_code_is_readable_and_whatever_is_pasted_resolves_to_it() -> None:
    code = new_invite_code()
    assert len(code) == 14 and code[4] == code[9] == "-"
    canonical = code.replace("-", "")
    assert all(c in INVITE_ALPHABET for c in canonical)
    assert new_invite_code() != code

    for pasted in (code, canonical, code.lower(), f"  {code[:4]} {code[5:9]} {code[10:]} ",
                   f"https://trypotluck.ai/circles/join?code={code}",
                   f"https://trypotluck.ai/circles/join#code={code}",
                   f"https://trypotluck.ai/circles/join?utm=x&code={code}#top"):
        assert normalise_invite_code(pasted) == canonical, pasted

    for not_a_code in ("", "hello", canonical[:-1], canonical + "A", "O" * 12, "ABCD-EFGH-1LOU"):
        assert normalise_invite_code(not_a_code) is None, not_a_code


# ---- creating, inviting, joining ---------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_holds_a_membership_from_the_start(sessions) -> None:
    owner = uuid.uuid4()
    circle = await sessions.circle(owner)
    assert circle.role == "owner"
    assert [(p.role, p.is_you) for p in circle.people] == [("owner", True)]
    assert [c.id for c in await sessions.listed(owner)] == [circle.id]


@pytest.mark.asyncio
async def test_an_invite_is_owner_only_and_only_its_hash_is_stored(sessions) -> None:
    owner, friend, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    created = await sessions.call(create_invite, circle.id, CreateInviteRequest(), member=owner)
    assert created.url == f"https://trypotluck.ai/circles/join#code={created.code}" and created.max_uses == 1
    async with sessions.factory() as db:
        mine = (await db.execute(
            select(CircleInvite).where(CircleInvite.circle_id == circle.id)
        )).scalars().all()
    assert len(mine) == 1
    assert mine[0].code_hash == hash_invite_code(normalise_invite_code(created.code))

    assert await sessions.status(create_invite, circle.id, None, member=stranger) == 404
    await sessions.join(created.code, friend)
    # Someone who joined cannot invite others on the owner's behalf.
    assert await sessions.status(create_invite, circle.id, None, member=friend) == 404


@pytest.mark.asyncio
async def test_a_preview_says_what_the_invite_is_for(sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    async with sessions.factory() as db:
        await db.execute(
            text("INSERT INTO members (id, email, display_name) VALUES (:id, :email, :name)"),
            {"id": owner, "email": f"owner-{owner.hex[:8]}@example.test", "name": "Ada"},
        )
        await db.commit()
    sessions.members.append(owner)
    circle = await sessions.circle(owner, "Home lab")
    code = await sessions.invite(circle.id, owner)

    preview = await sessions.call(preview_invite, AcceptInviteRequest(code=code.lower()), member=friend)
    assert (preview.circle_name, preview.invited_by, preview.people, preview.already_in) == ("Home lab", "Ada", 1, False)
    assert (await sessions.call(preview_invite, AcceptInviteRequest(code=code), member=owner)).already_in is True
    assert await sessions.status(preview_invite, AcceptInviteRequest(code="AAAA-BBBB-CCCC"), member=friend) == 404
    # Inside the circle the owner is shown by the name they set.
    joined = await sessions.join(code, friend)
    assert [p.name for p in joined.people if p.role == "owner"] == ["Ada"]


@pytest.mark.asyncio
async def test_accepting_joins_once_and_a_single_use_invite_admits_one_person(sessions) -> None:
    owner, friend, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    code = await sessions.invite(circle.id, owner)

    joined = await sessions.join(code, friend)
    assert joined.id == circle.id and joined.role == "member"
    assert sorted((p.role, p.is_you) for p in joined.people) == [("member", True), ("owner", False)]
    assert joined.members == [], "pasted keys are the owner's business"

    assert [(c.id, c.role) for c in await sessions.listed(friend)] == [(circle.id, "member")]
    assert len((await sessions.listed(owner))[0].people) == 2

    # The used-up invite is gone for everyone, including the person it let in.
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=code), member=third) == 404
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=code), member=friend) == 404
    assert await sessions.call(list_invites, circle.id, member=owner) == []


@pytest.mark.asyncio
async def test_accepting_again_changes_nothing_and_keeps_the_invite(sessions) -> None:
    owner, friend, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    code = await sessions.invite(circle.id, owner, max_uses=2)
    await sessions.join(code, friend)
    again = await sessions.join(code, friend)
    assert again.role == "member" and len(again.people) == 2
    # The owner following their own link is not a join either.
    assert (await sessions.join(code, owner)).role == "owner"
    assert [i.uses for i in await sessions.call(list_invites, circle.id, member=owner)] == [1]
    assert (await sessions.join(code, third)).role == "member"


@pytest.mark.asyncio
async def test_expired_and_withdrawn_invites_admit_nobody(sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    expired = await sessions.invite(circle.id, owner)
    async with sessions.factory() as db:
        await db.execute(
            update(CircleInvite).where(CircleInvite.code_hash == hash_invite_code(normalise_invite_code(expired)))
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await db.commit()
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=expired), member=friend) == 404

    withdrawn = await sessions.invite(circle.id, owner, max_uses=5)
    invite_id = (await sessions.call(list_invites, circle.id, member=owner))[0].id
    await sessions.call(revoke_invite, circle.id, invite_id, member=owner)
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=withdrawn), member=friend) == 404
    assert await sessions.listed(friend) == []


@pytest.mark.asyncio
async def test_a_full_circle_takes_no_one_else(sessions, monkeypatch) -> None:
    monkeypatch.setattr(circles_router, "MAX_CIRCLE_PEOPLE", 2)
    owner = uuid.uuid4()
    circle = await sessions.circle(owner)
    code = await sessions.invite(circle.id, owner, max_uses=5)
    await sessions.join(code, uuid.uuid4())
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=code), member=uuid.uuid4()) == 409
    assert [i.uses for i in await sessions.call(list_invites, circle.id, member=owner)] == [1]


# ---- leaving and removing ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_can_leave_and_the_owner_cannot(sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    await sessions.join(await sessions.invite(circle.id, owner), friend)

    assert await sessions.status(leave_circle, circle.id, member=owner) == 409
    await sessions.call(leave_circle, circle.id, member=friend)
    assert await sessions.listed(friend) == []
    assert len((await sessions.listed(owner))[0].people) == 1
    # Gone means gone: the circle now looks like any circle they are not in.
    assert await sessions.status(leave_circle, circle.id, member=friend) == 404
    assert await sessions.status(circle_availability, circle.id, member=friend) == 404


@pytest.mark.asyncio
async def test_the_owner_removes_people_but_not_themselves(sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    joined = await sessions.join(await sessions.invite(circle.id, owner), friend)
    friend_row = next(p for p in joined.people if p.is_you)
    owner_row = next(p for p in joined.people if p.role == "owner")

    assert await sessions.status(remove_person, circle.id, owner_row.id, member=friend) == 404
    await sessions.call(remove_person, circle.id, owner_row.id, member=owner)   # no effect
    await sessions.call(remove_person, circle.id, friend_row.id, member=owner)
    people = (await sessions.listed(owner))[0].people
    assert [(p.role, p.is_you) for p in people] == [("owner", True)]
    assert await sessions.listed(friend) == []


@pytest.mark.asyncio
async def test_deleting_the_circle_ends_it_for_everyone(sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    code = await sessions.invite(circle.id, owner, max_uses=3)
    await sessions.join(code, friend)
    assert await sessions.status(delete_circle, circle.id, member=friend) == 404
    await sessions.call(delete_circle, circle.id, member=owner)
    async with sessions.factory() as db:
        left = (await db.execute(
            select(CircleMembership).where(CircleMembership.circle_id == circle.id)
        )).scalars().all()
    assert left == []
    assert await sessions.listed(friend) == []
    assert await sessions.status(accept_invite, AcceptInviteRequest(code=code), member=uuid.uuid4()) == 404


# ---- who lends to whom ---------------------------------------------------------


@pytest.mark.asyncio
async def test_lenders_are_everyone_in_the_circle_but_the_requester(sessions) -> None:
    owner, friend, keyed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    circle = await sessions.circle(owner)
    await sessions.join(await sessions.invite(circle.id, owner), friend)
    # Someone the owner added by machine key with an older app still lends.
    key = f"legacy-{uuid.uuid4().hex}"
    sessions.keys.append(key)
    async with sessions.factory() as db:
        db.add(MemberKey(public_key=key, member_id=keyed, added_via="seed"))
        db.add(CircleMember(circle_id=circle.id, member_pubkey=key))
        db.add(CircleMember(circle_id=circle.id, member_pubkey="a-key-nobody-registered"))
        await db.commit()
    async with sessions.factory() as db:
        assert await lender_member_ids(db, circle.id, owner) == {friend, keyed}
        assert await lender_member_ids(db, circle.id, friend) == {owner, keyed}


async def _machine(session_factory, member_id: uuid.UUID, model: str, **flags) -> uuid.UUID:
    async with session_factory.factory() as db:
        async with db.begin():
            peer = Peer(member_id=member_id, public_key=f"inv-{uuid.uuid4().hex}", hardware_summary={}, status="active")
            db.add(peer)
            await db.flush()
            db.add(PeerCapability(peer_id=peer.id, model_slug=model, quantization="q4"))
    session_factory.track_peer(peer.id)
    await register_peer(PeerConnection(peer_id=peer.id, session_id=uuid.uuid4(), ws=_WS(), member_id=member_id, **flags))
    return peer.id


@pytest.mark.asyncio
async def test_a_circle_works_in_both_directions_and_stops_when_someone_leaves(session_factory, sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    model = f"inv-model-{uuid.uuid4().hex[:8]}"
    circle = await sessions.circle(owner)
    await sessions.join(await sessions.invite(circle.id, owner), friend)
    owner_machine = await _machine(session_factory, owner, model)
    friend_machine = await _machine(session_factory, friend, model)

    async def borrow(requester):
        async with session_factory.factory() as db:
            lenders = await lender_member_ids(db, circle.id, requester)
            peer, scope = await select_peer_for_request(
                db, model_slug=model, requester_member_id=requester, scope="circle",
                circle_member_ids=lenders,
            )
            return (peer.peer_id if peer else None), scope

    try:
        # The owner borrows the friend's machine, and the friend the owner's.
        # Neither is handed their own machine by a circle request.
        assert await borrow(owner) == (friend_machine, "circle")
        assert await borrow(friend) == (owner_machine, "circle")

        async with session_factory.factory() as db:
            availability = await circle_availability(circle.id, db=db, member_id=owner)
            assert [(m.slug, m.machines) for m in availability.models] == [(model, 1)]

        async with session_factory.factory() as db:
            await leave_circle(circle.id, db=db, member_id=friend)
        assert await borrow(owner) == (None, None), "their machine is no longer the circle's to use"
        async with session_factory.factory() as db:
            assert (await circle_availability(circle.id, db=db, member_id=owner)).models == []
    finally:
        await unregister_peer(owner_machine)
        await unregister_peer(friend_machine)


@pytest.mark.asyncio
async def test_a_member_who_does_not_share_with_circles_is_not_lent_out(session_factory, sessions) -> None:
    owner, friend = uuid.uuid4(), uuid.uuid4()
    model = f"inv-model-{uuid.uuid4().hex[:8]}"
    circle = await sessions.circle(owner)
    await sessions.join(await sessions.invite(circle.id, owner), friend)
    friend_machine = await _machine(session_factory, friend, model, serves_circle=False)
    try:
        async with session_factory.factory() as db:
            peer, _ = await select_peer_for_request(
                db, model_slug=model, requester_member_id=owner, scope="circle",
                circle_member_ids=await lender_member_ids(db, circle.id, owner),
            )
        assert peer is None, "joining a circle is not the same as sharing a machine with it"
    finally:
        await unregister_peer(friend_machine)
