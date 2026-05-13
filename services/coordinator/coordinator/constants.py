"""Coordinator-wide constants.

Nothing here is part of the wire protocol — these are internal values
shared across routers that would otherwise be copy-pasted.
"""
from __future__ import annotations

import uuid

# Stub member identity used by every endpoint that needs a member_id before
# real auth lands in v0.5. Matches the dev-fixture seed already in
# contributor_releases. When auth ships, every reference to this constant
# is replaced by the value from the authenticated session.
STUB_MEMBER_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
