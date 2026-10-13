"""Per-node bearer-token auth for the mesh machine-plumbing routes.

Companion to `middleware/auth.py` (the machine-wide coordinator token) and
`middleware/session_auth.py` (the member-session cookie/bearer). This module
closes the third leg: an app-spawned mesh sidecar should never need the
machine-wide `POTLUCK_COORDINATOR_TOKEN` for its own ongoing calls - it gets
a `node_token` once, at `POST /mesh/enroll`, and uses that from then on.

Storage + lookup mirrors `session_auth.py`'s pattern exactly: the coordinator
stores only `SHA-256(node_token)` (in `mesh_nodes.node_token_hash`), and a
presented token is hashed and matched against that column. The raw token is
shown to the caller exactly once, in the enroll response, and is never
logged.

Wire contract (exact - the Go sidecar must match byte-for-byte):

  * Header: ``Authorization: Bearer <node_token>`` - both on ordinary HTTP
    requests to the machine routes (POST /mesh/heartbeat, GET /mesh/peers,
    POST /mesh/derp-token, DELETE /mesh/nodes/{id}) and on the `/mesh/signal`
    WebSocket upgrade request (same header name/format, sent as one of the
    upgrade request's HTTP headers).
  * The legacy ``X-Potluck-Coordinator-Token`` header keeps working,
    unchanged, on every one of those routes (back-compat for machines that
    have no per-node token).
  * A request MAY present either credential. If ``Authorization: Bearer``
    resolves to an enrolled node, that node's identity is bound to the
    request - the caller may only act as that node (any wg_public_key/
    node_id supplied elsewhere in the request must match, or the route
    returns 403). If it doesn't resolve (missing, malformed, unknown, or
    revoked), the request falls back to the coordinator-token check. If
    neither is valid, 401.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy import select

from coordinator.db.database import get_session_factory
from coordinator.middleware.auth import coordinator_token_matches
from coordinator.models.mesh_node import MeshNode


def hash_node_token(raw_token: str) -> str:
    """SHA-256 hex digest of a raw node_token - the only form persisted in
    `mesh_nodes.node_token_hash`. Shared by the enroll-time mint (which
    stores the hash) and every lookup here (which hashes the presented
    bearer token before querying), so the two never drift."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def extract_bearer_token(authorization: str | None) -> str | None:
    """Pull the raw token out of an ``Authorization: Bearer <token>``
    header value. Returns None for any other shape (absent, wrong scheme,
    empty). Shared by the HTTP dependency below and the `/mesh/signal` WS
    upgrade handler, which reads headers directly instead of through
    FastAPI's dependency injection."""
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[len("bearer ") :].strip()
        return candidate or None
    return None


async def resolve_node_from_token(
    authorization: Annotated[str | None, Header()] = None,
    x_potluck_coordinator_token: Annotated[
        str | None, Header(alias="X-Potluck-Coordinator-Token")
    ] = None,
) -> MeshNode | None:
    """FastAPI dependency gating the mesh machine-plumbing routes.

    Returns:
      * a `MeshNode` - the caller presented a valid `Authorization: Bearer
        <node_token>` that resolved (by hash) to exactly that enrolled node.
        Callers MUST enforce that any node identity supplied elsewhere in
        the request (wg_public_key, node_id, ...) matches this node - a
        node_token must only ever act as its own node.
      * `None` - the caller authenticated via the legacy
        `X-Potluck-Coordinator-Token` (or dev-mode passthrough, when
        `POTLUCK_COORDINATOR_TOKEN` is unset). No node identity is bound;
        the route falls back to trusting the wg_public_key/node_id supplied
        in the request body/query.

    Raises:
        HTTPException 401 - neither credential is valid.

    Deliberately does not take `db: AsyncSession = Depends(get_db)` - that
    would make FastAPI resolve a live database session on every call to a
    gated route, even a bare "no credentials at all" 401, which breaks the
    no-DB auth-rejection tests every other coordinator auth dependency
    supports (`require_coordinator_token`, `resolve_session_member_id`'s
    dev-passthrough path). Instead this opens its own short-lived session
    via `get_session_factory()` (mirrors `/mesh/signal`'s own pattern),
    and only when a bearer token is actually present to look up.
    """
    bearer = extract_bearer_token(authorization)
    if bearer:
        token_hash = hash_node_token(bearer)
        async with get_session_factory()() as db:
            result = await db.execute(
                select(MeshNode).where(MeshNode.node_token_hash == token_hash)
            )
            node = result.scalar_one_or_none()
        if node is not None:
            return node
        # Bearer present but doesn't resolve to any enrolled node - don't
        # hard-fail here, fall through to the coordinator-token check below.
        # (A caller that races enroll/rotation, or simply hasn't upgraded to
        # sending its node_token correctly yet, still gets a fair shot at
        # the back-compat credential in the same request.)

    if coordinator_token_matches(x_potluck_coordinator_token):
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="unauthorized",
    )


def enforce_node_scope(auth_node: MeshNode | None, caller_node_id: uuid.UUID) -> None:
    """Raise 403 if a resolved node_token is trying to act as a different
    node than the one the request otherwise identifies (by wg_public_key or
    node_id). No-op when `auth_node` is None (coordinator-token / dev-mode
    caller - unchanged back-compat behaviour, no per-node binding to
    enforce)."""
    if auth_node is not None and auth_node.id != caller_node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="node_token does not authorize this node",
        )
