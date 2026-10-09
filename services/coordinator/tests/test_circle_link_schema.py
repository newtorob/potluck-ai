"""The circle_links table, as migration 0014 builds it.

The database is the last line of defence for the rules the service relies on:
a link cannot join a member to themselves or a machine to itself, a port is a
port, the end columns come as a pair with a known reason, a pair has one live
link, and a token hash is never shared. Also that the migration and the model
agree, that 0014 sits on 0013 as the single head, and that it is additive.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from coordinator.db.base import Base
from coordinator.models import CircleLink, Household, MeshNode, TrustedCircle
from coordinator.models.circle_link import END_REASONS
from tests.circle_link_helpers import fresh_address, wg_key

COORDINATOR_ROOT = Path(__file__).resolve().parent.parent


async def _world(db):
    """Two households with two machines in the first, a circle, all in the open
    transaction (the db_session fixture rolls it back)."""
    taken: set = set()
    a, _ = await fresh_address(db, taken)
    b, _ = await fresh_address(db, taken)
    ma, mb = uuid.uuid4(), uuid.uuid4()
    ha = Household(owner_member_id=ma, name="a", mesh_cidr=a)
    hb = Household(owner_member_id=mb, name="b", mesh_cidr=b)
    db.add_all([ha, hb])
    await db.flush()
    nodes = [
        MeshNode(household_id=ha.id, node_name="a1", mesh_ip=a.replace(".0/24", ".2"), wg_public_key=wg_key()),
        MeshNode(household_id=ha.id, node_name="a2", mesh_ip=a.replace(".0/24", ".3"), wg_public_key=wg_key()),
        MeshNode(household_id=hb.id, node_name="b1", mesh_ip=b.replace(".0/24", ".2"), wg_public_key=wg_key()),
    ]
    circle = TrustedCircle(owner_member_id=ma, name="c")
    db.add_all([*nodes, circle])
    await db.flush()
    return ma, mb, nodes, circle


def _link(circle, req_member, req_node, lend_member, lend_node, **over):
    now = datetime.now(timezone.utc)
    fields = dict(
        circle_id=circle.id, requester_member_id=req_member, requester_node_id=req_node.id,
        lender_member_id=lend_member, lender_node_id=lend_node.id, lender_port=8340,
        model_slug="m", token_hash=uuid.uuid4().hex + uuid.uuid4().hex, created_at=now,
        expires_at=now + timedelta(minutes=10), renewals=0,
    )
    fields.update(over)
    return CircleLink(**fields)


async def _refuses(db, link, constraint: str) -> None:
    """The insert fails on `constraint` and leaves the surrounding transaction usable.
    (The add happens inside the savepoint: opening one flushes what is pending.)"""
    with pytest.raises(IntegrityError) as exc:
        async with db.begin_nested():
            db.add(link)
            await db.flush()
    assert constraint in str(exc.value), str(exc.value)[:300]


@pytest.mark.asyncio
async def test_a_good_row_is_accepted_and_defaults_are_the_expected_ones(db_session) -> None:
    ma, mb, (a1, a2, b1), circle = await _world(db_session)
    link = _link(circle, ma, a1, mb, b1)
    db_session.add(link)
    await db_session.flush()
    row = (await db_session.execute(select(CircleLink).where(CircleLink.id == link.id))).scalar_one()
    assert (row.renewals, row.ended_at, row.end_reason, row.renewed_at, row.lender_peer_id) == (0, None, None, None, None)


@pytest.mark.asyncio
async def test_check_constraints(db_session) -> None:
    ma, mb, (a1, a2, b1), circle = await _world(db_session)
    now = datetime.now(timezone.utc)
    await _refuses(db_session, _link(circle, ma, a1, ma, b1), "ck_circle_links_distinct_members")
    await _refuses(db_session, _link(circle, ma, a1, mb, a1), "ck_circle_links_distinct_nodes")
    for port in (0, 65536, -1):
        await _refuses(db_session, _link(circle, ma, a1, mb, b1, lender_port=port), "ck_circle_links_port")
    await _refuses(db_session, _link(circle, ma, a1, mb, b1, expires_at=now - timedelta(seconds=1)),
                   "ck_circle_links_expiry_after_creation")
    await _refuses(db_session, _link(circle, ma, a1, mb, b1, ended_at=now), "ck_circle_links_end_pair")
    await _refuses(db_session, _link(circle, ma, a1, mb, b1, end_reason="expired"), "ck_circle_links_end_pair")
    await _refuses(db_session, _link(circle, ma, a1, mb, b1, ended_at=now, end_reason="because"),
                   "ck_circle_links_end_reason")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", END_REASONS)
async def test_every_end_reason_the_service_uses_is_allowed(db_session, reason) -> None:
    ma, mb, (a1, a2, b1), circle = await _world(db_session)
    db_session.add(_link(circle, ma, a1, mb, b1, ended_at=datetime.now(timezone.utc), end_reason=reason))
    await db_session.flush()


@pytest.mark.asyncio
async def test_one_live_link_per_pair_and_a_unique_token(db_session) -> None:
    ma, mb, (a1, a2, b1), circle = await _world(db_session)
    first = _link(circle, ma, a1, mb, b1)
    db_session.add(first)
    await db_session.flush()
    await _refuses(db_session, _link(circle, ma, a1, mb, b1), "uq_circle_links_live_pair")
    # A different pair is fine, and so is the same pair once the first has ended.
    db_session.add(_link(circle, ma, a2, mb, b1))
    await db_session.flush()
    first.ended_at, first.end_reason = datetime.now(timezone.utc), "ended"
    await db_session.flush()
    db_session.add(_link(circle, ma, a1, mb, b1))
    await db_session.flush()
    await _refuses(db_session, _link(circle, ma, a1, mb, b1, token_hash=first.token_hash), "uq_circle_links_token_hash")


@pytest.mark.asyncio
async def test_rows_go_with_their_circle_or_their_machine(db_session) -> None:
    ma, mb, (a1, a2, b1), circle = await _world(db_session)
    kept = _link(circle, ma, a1, mb, b1)
    gone_with_node = _link(circle, ma, a2, mb, b1)
    db_session.add_all([kept, gone_with_node])
    await db_session.flush()

    await db_session.execute(text("DELETE FROM mesh_nodes WHERE id = :i"), {"i": a2.id})
    ids = {r[0] for r in (await db_session.execute(text("SELECT id FROM circle_links"))).all()}
    assert ids == {kept.id}

    await db_session.execute(text("DELETE FROM trusted_circles WHERE id = :i"), {"i": circle.id})
    assert (await db_session.execute(text("SELECT count(*) FROM circle_links"))).scalar_one() == 0


# ---- the migration itself ---------------------------------------------------


def _script() -> ScriptDirectory:
    from alembic.config import Config

    cfg = Config(str(COORDINATOR_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(COORDINATOR_ROOT / "coordinator" / "db" / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_0014_sits_on_0013_and_is_the_only_head() -> None:
    script = _script()
    assert script.get_heads() == ["0014_circle_links"]
    rev = script.get_revision("0014_circle_links")
    assert rev.down_revision == "0013_circle_memberships_invites"


def test_0014_is_additive_and_has_a_downgrade() -> None:
    source = (COORDINATOR_ROOT / "coordinator/db/migrations/versions/0014_circle_links.py").read_text()
    up, down = source.split("def downgrade()")
    assert "create_table(" in up and up.count("create_table(") == 1
    assert "circle_links" in up
    for destructive in ("drop_table", "drop_column", "alter_column", "execute("):
        assert destructive not in up, f"upgrade must be additive: found {destructive}"
    assert "drop_table(\"circle_links\")" in down


@pytest.mark.asyncio
async def test_the_migrated_table_matches_the_model(session_factory) -> None:
    """compare_metadata finds nothing to change for circle_links: the database
    that alembic upgraded and the model the code uses are the same table."""
    async with session_factory.factory() as db:
        conn = await db.connection()

        def diff(sync_conn):
            ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
            return compare_metadata(ctx, Base.metadata)

        found = await conn.run_sync(diff)
    mine = [d for d in found if "circle_links" in repr(d)]
    assert mine == [], mine
