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

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, ConfigDict
from potluck_local.services.contribution_policy import ContributionSchedule, admission_reason

logger = logging.getLogger(__name__)
router = APIRouter()


class ContributionControls(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    audience: Literal['mine', 'circles', 'everyone'] | None = None
    schedule: ContributionSchedule | None = None
    maxJobs: int | None = Field(default=None, ge=0, le=64)
    maxTokens: int | None = Field(default=None, ge=1, le=32768)
    minFreeMemoryGiB: float | None = Field(default=None, ge=0, le=1024)


@router.get('/controls')
async def contribution_controls() -> dict:
    from potluck_local.services.settings_manager import get_settings
    from potluck_local.services.contributor_service import get_contributor_service, sharing_audiences
    network = get_settings().network
    svc = get_contributor_service()
    status = svc.status()
    return {'schemaVersion': 1, 'enabled': network.contributor_mode,
            'audience': network.contributor_audience, 'servingAudiences': sharing_audiences(network),
            'schedule': network.contributor_schedule, 'maxJobs': network.max_concurrent_jobs,
            'maxTokens': network.contributor_max_tokens, 'minFreeMemoryGiB': network.contributor_min_free_memory_gib,
            'state': status['state'], 'acceptingJobs': status['accepting_jobs'],
            'pauseReason': admission_reason(network), 'signedIn': svc.has_member_session(),
            'jobsInFlight': status['jobs_in_flight'], 'jobsServed': status['jobs_served'],
            'lastError': status['last_error']}


@router.patch('/controls')
async def set_contribution_controls(body: ContributionControls) -> dict:
    from potluck_local.routers.settings import update_settings
    fields = {'enabled': 'contributor_mode', 'audience': 'contributor_audience',
              'schedule': 'contributor_schedule',
              'maxJobs': 'max_concurrent_jobs', 'maxTokens': 'contributor_max_tokens',
              'minFreeMemoryGiB': 'contributor_min_free_memory_gib'}
    patch = {fields[key]: value for key, value in body.model_dump(exclude_unset=True).items()}
    if any(value is None and key not in ('contributor_schedule', 'contributor_max_tokens', 'contributor_audience')
           for key, value in patch.items()):
        raise HTTPException(422, 'Only audience, schedule and maxTokens can be cleared.')
    await update_settings({'network': patch})
    return await contribution_controls()


# ---------------------------------------------------------------------------
# In-process contributor: status + member session
# ---------------------------------------------------------------------------


@router.get("/status")
async def contributor_status() -> dict[str, Any]:
    """What the Contribute compute toggle is doing right now: state, this
    peer's own id, heartbeat and job counters, the last error category.
    Counts and our own ids only; nothing about requesters or other peers."""
    from potluck_local.services.contributor_service import get_contributor_service

    svc = get_contributor_service()
    body = svc.status()
    body["has_member_session"] = svc.has_member_session()
    return body


class MemberSessionBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.put("/session", status_code=204)
async def put_member_session(body: MemberSessionBody) -> Response:
    """Hand the sidecar the member's session bearer so the contributor can
    identify itself to the coordinator. Held in memory only; never logged,
    never persisted, never echoed back."""
    from potluck_local.services.contributor_service import get_contributor_service

    get_contributor_service().set_member_session(body.token)
    return Response(status_code=204)


@router.delete("/session", status_code=204)
async def delete_member_session() -> Response:
    from potluck_local.services.contributor_service import get_contributor_service

    get_contributor_service().set_member_session(None)
    return Response(status_code=204)


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
