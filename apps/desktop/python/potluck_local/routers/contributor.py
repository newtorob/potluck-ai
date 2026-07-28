"""Contributor device-key read endpoint (loopback-only, local frontend use).

Exposes this machine's Ed25519 *device* public key — the same key
`potluck_local.contributor.client` presents as `peer_public_key` in its WS
attestation handshake to the coordinator (see `contributor/client.py`,
`contributor/keys.py`) — to the local frontend.

Why this exists: the coordinator can run with `REQUIRE_REGISTERED_KEYS=true`
(the prod Fly posture), which hard-rejects attestation from any device key
that hasn't been pre-registered via the coordinator's session-authed
`POST /me/device-keys` (services/coordinator/coordinator/routers/circles.py).
Registration has to happen from the frontend — it holds the member's login
session; the contributor process itself has no session and no coordinator
credential of its own. This endpoint is the bridge: the frontend reads the
key here, then POSTs it to the coordinator itself (see
frontend/src/lib/deviceKey.ts).

Key generation is decoupled from the contributor WS client's lifecycle —
`ensure_keypair` only touches the on-disk PEM file, so calling this endpoint
lazily generates (or loads) the SAME key file `contributor.client.main()`
would use, without needing the contributor process to be running. Both read
`contributor.config.get_settings().key_dir`, which resolves to the same
platform-conventional path either way (see keys.py / config.py docstrings).

Access: no explicit gate here — this router is mounted like every other
non-`/peer/*` router, so `app.py`'s `peer_access_middleware` already
restricts it to loopback callers (+ the sidecar bearer token in production).
A device public key is not secret, but there's no reason to widen the
surface beyond the existing local-only posture.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()


class DeviceKeyResponse(BaseModel):
    public_key: str


@router.get("/device-key", response_model=DeviceKeyResponse)
async def get_device_key() -> DeviceKeyResponse:
    """Ensure + return this machine's contributor Ed25519 public key
    (base64url, no padding — same encoding `AttestationPayload.peer_public_key`
    uses on the wire). Lazily generates the keypair on first call, matching
    `contributor.client.main()`'s own startup behaviour."""
    try:
        from potluck_local.contributor.config import get_settings as get_contributor_settings
        from potluck_local.contributor.keys import ensure_keypair, public_key_b64

        contributor_settings = get_contributor_settings()
        priv = ensure_keypair(contributor_settings.key_dir)
        return DeviceKeyResponse(public_key=public_key_b64(priv.public_key()))
    except Exception as exc:  # pragma: no cover — defensive (e.g. unwritable key_dir)
        logger.exception("contributor_device_key_read_failed")
        raise HTTPException(status_code=500, detail=f"could not read device key: {exc}")
