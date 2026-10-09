"""Wire types for the circle link HTTP endpoints (routers/circle_links.py).

The shapes are pinned to tests/fixtures/circle_link_shapes.json by
tests/test_circle_link_contract.py. The sidecar (Go) and the engine (Python)
are written against that file, so a change here is a change to both.

Privacy: the requester learns the lender's mesh address and port, which it
needs to connect, and nothing else about the lender: no member, machine
name, node id, peer id or hostname. The token is returned once, here, and
only its hash is stored.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CircleLinkCreateRequest(BaseModel):
    """Body of POST /me/circles/{circle_id}/links."""

    # The requester's own machine, from its sidecar's /status node_id.
    requester_node_id: uuid.UUID
    model_slug: str = Field(..., min_length=1, max_length=128)
    # Links the requester just failed on. The lenders behind them are skipped.
    # Only links that belong to the caller count; others are ignored.
    exclude_link_ids: list[uuid.UUID] = Field(default_factory=list, max_length=4)


class CircleLinkLender(BaseModel):
    """Where the requester connects: the lender machine's mesh address and
    the port its engine's circle listener is bound to."""

    mesh_ip: str
    port: int


class CircleLinkCreateResponse(BaseModel):
    """201 body of a create. `token` appears here and nowhere else; the
    requester sends it as a Bearer token to the lender's circle listener."""

    link_id: uuid.UUID
    circle_id: uuid.UUID
    model_slug: str
    token: str
    lender: CircleLinkLender
    ttl_s: int
    renew_after_s: int
    expires_at: datetime
    # True when both sidecars acknowledged the link. A create that could not
    # get both acknowledgements is an error, never a 201 with ready false.
    ready: bool = True


class CircleLinkDelivered(BaseModel):
    """Whether each sidecar acknowledged a renewal."""

    requester: bool
    lender: bool


class CircleLinkRenewResponse(BaseModel):
    """200 body of a renew."""

    link_id: uuid.UUID
    ttl_s: int
    renew_after_s: int
    expires_at: datetime
    delivered: CircleLinkDelivered


class CircleLinkErrorDetail(BaseModel):
    """The `detail` of every error these endpoints raise themselves.

    `code` is for programs and `message` is for people. `reason` is set for
    two codes: sidecar_rejected (the sidecar's reason) and link_ended (why the
    link ended); null otherwise. Errors the framework raises (401, 422) keep
    their usual shape.
    """

    code: str
    message: str
    reason: str | None = None


class CircleLinkErrorBody(BaseModel):
    detail: CircleLinkErrorDetail
