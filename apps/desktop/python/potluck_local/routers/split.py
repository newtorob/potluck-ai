"""Authenticated local controls and narrow leased household transport."""
from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from potluck_local.routers.models import LockedModel
from potluck_local.services.split_manager import get_split_manager, paired_host
from potluck_local.services.split_native import SplitError

router = APIRouter()
peer_router = APIRouter()


class SplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: LockedModel
    peers: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=3)
    context: int = Field(default=2048, ge=512, le=8192)


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    request_id: UUID


def token(request):
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else ""


@router.get("/status")
async def status():
    return get_split_manager().status()


@router.post("/worker")
async def worker(body: WorkerRequest):
    return await get_split_manager().enable_worker(body.enabled)


@router.post("/plan")
async def plan(body: SplitRequest):
    return await get_split_manager().plan(body.model.model_dump(), body.peers, body.context)


@router.post("/start", status_code=202)
async def start(body: SplitRequest):
    return await get_split_manager().start(body.model.model_dump(), body.peers, body.context)


@router.post("/stop")
async def stop():
    return await get_split_manager().stop()


@peer_router.get("/status")
async def peer_status(request: Request):
    if not request.client or not await paired_host(request.client.host):
        raise HTTPException(403, "Only current owned mesh members can discover split workers.")
    value = get_split_manager().status()
    # Household peers discover capacity, never local model paths/session state.
    return {key: value[key] for key in ("protocol", "engine", "available", "freeMemoryBytes", "unifiedMemory")
            if key in value}


@peer_router.post("/lease")
async def acquire(body: LeaseRequest, request: Request):
    return await get_split_manager().acquire(request.client.host if request.client else "",
                                           body.model_dump(mode="json"))


@peer_router.post("/lease/renew")
async def renew(request: Request):
    return await get_split_manager().renew(request.client.host if request.client else "", token(request))


@peer_router.delete("/lease")
async def release(request: Request):
    return await get_split_manager().release(request.client.host if request.client else "", token(request))


@peer_router.websocket("/tunnel")
async def tunnel(websocket: WebSocket):
    # HTTP middleware doesn't protect WebSockets. The manager checks live
    # membership, exact source address, lease secret, expiry and opt-in itself.
    try:
        await get_split_manager().worker_tunnel(websocket, token(websocket))
    except Exception:
        try:
            await websocket.close(code=1008)
        except Exception:
            pass
