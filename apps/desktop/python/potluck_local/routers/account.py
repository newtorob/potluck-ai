"""Authenticated local account control; never returns member or device bearers."""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from potluck_local.services.member_account import get_member_account

router = APIRouter()


class SessionBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class LogoutBody(BaseModel):
    local_only: bool = False


class HouseholdBody(BaseModel):
    name: str = Field(default="My machines", min_length=1, max_length=64)


@router.get("")
async def status():
    return get_member_account().status()


@router.put("/session")
async def commit(body: SessionBody):
    return await get_member_account().commit(body.token)


@router.post("/session/import")
async def import_session(body: SessionBody):
    return await get_member_account().commit(body.token, import_only=True)


@router.post("/login/start")
async def start_login():
    return await get_member_account().start_login()


@router.post("/login/poll")
async def poll_login():
    return await get_member_account().poll_login()


@router.post("/logout")
async def logout(body: LogoutBody):
    return await get_member_account().logout(local_only=body.local_only)


@router.get("/household")
async def household():
    return await get_member_account().household("list")


@router.post("/household")
async def create_household(body: HouseholdBody):
    return await get_member_account().household("create", body.model_dump())


@router.post("/household/invite")
async def invite():
    return await get_member_account().household("invite")
