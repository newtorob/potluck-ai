"""010 — case-insensitive unique index on members.email

members.email was case-sensitive-unique only (uq_members_email /
ix_members_email), so "rob@x.com" and "Rob@x.com" could exist as two
separate member rows. AuthService now looks members up by lower(email) and
normalizes every new member's stored email to lowercase, so this index makes
that the enforced invariant at the database level too, not just app-level
convention.

DUPLICATE-DATA EDGE CASE: if members already contains case-insensitive
duplicates (e.g. two rows differing only in email case), this
CREATE UNIQUE INDEX will fail with a uniqueness violation. Before deploying,
check for that with:

    SELECT lower(email), count(*)
    FROM members
    GROUP BY lower(email)
    HAVING count(*) > 1;

There is no automated merge here on purpose — merging two member rows means
reassigning their sessions/memberships/audit rows to a single survivor,
which is a judgment call (which row is canonical, what happens to Stripe
customer IDs) that should not happen silently inside a schema migration.
Resolve any hits manually first.

Revision ID: 0010_members_email_ci_unique
Revises: 0009_consumed_magic_links
Create Date: 2026-07-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_members_email_ci_unique"
down_revision: str = "0009_consumed_magic_links"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "uq_members_email_lower",
        "members",
        [sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_members_email_lower", table_name="members")
