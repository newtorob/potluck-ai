"""Trusted circles from this machine, as the signed-in member.

The desktop app talks to the coordinator about circles itself. A machine
with only the CLI has nothing to do that, so the engine does it here with
the member session it already holds: list, create, invite, join, leave.
A circle is named in a path by its name or its ID.

Nothing here handles a request or its content; that is the gateway
(``X-Potluck-Scope: circle``). These calls carry only circle and invite
metadata.
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from potluck_local.services import pool_routing

router = APIRouter()

# Statuses whose message is meant for the person at the terminal.
_PASS_THROUGH = (400, 404, 409, 422)


def _fail(status: int, detail: str, code: str) -> HTTPException:
    """An error the CLI can explain. The CLI never prints a response body
    (it could carry anything), so the reason also travels as a short code."""
    return HTTPException(status, detail, headers={"X-Potluck-Error": code})


def _code_for(method: str, path: str, status: int) -> str:
    if "/invites/" in path:
        return "circle_full" if status == 409 else "invite_invalid"
    if path.endswith("/membership") and status == 409:
        return "owner_cannot_leave"
    if status == 404:
        return "circle_not_found"
    return "circle_refused"


async def _coordinator(method: str, path: str, body: dict | None = None) -> Any:
    base = pool_routing.coordinator_http_base()
    headers = pool_routing._headers(base)
    if "Authorization" not in headers:
        raise _fail(401, "Sign in to use trusted circles: potluck login.", "signed_out")
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.request(
                method, f"{base}{path}", json=body, headers={**headers, "Accept": "application/json"}
            )
    except httpx.HTTPError:
        raise _fail(502, "Could not reach the coordinator. Retry when connected.", "coordinator_unreachable") from None
    if resp.status_code == 401:
        raise _fail(401, "Your sign-in has expired. Run potluck login.", "sign_in_expired")
    if resp.status_code >= 400:
        detail = None
        try:
            detail = resp.json().get("detail")
        except Exception:  # noqa: BLE001
            pass
        status = resp.status_code if resp.status_code in _PASS_THROUGH else 502
        raise _fail(
            status,
            detail if isinstance(detail, str) else "The coordinator refused the request.",
            _code_for(method, path, resp.status_code) if status != 502 else "coordinator_unreachable",
        )
    # The member's circles just changed or may have: do not answer the next
    # gateway request from a list read before this call.
    if method != "GET":
        pool_routing._circle_cache.clear()
    return resp.json() if resp.content else {}


_SELECTION_CODES = {401: "signed_out", 404: "circle_not_found", 400: "circle_ambiguous", 409: "no_circles"}


async def _circle_id(selector: str) -> str:
    try:
        return await pool_routing.resolve_circle_id(selector)
    except pool_routing.CircleSelectionError as exc:
        raise _fail(exc.status_code, exc.detail, _SELECTION_CODES.get(exc.status_code, "coordinator_unreachable")) from exc


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_days: int = Field(default=7, ge=1, le=30)
    max_uses: int = Field(default=1, ge=1, le=20)


class CodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=512)


@router.get("")
async def list_circles() -> dict:
    """Every circle the member is in, each with the models its other members
    are offering right now."""
    circles = (await _coordinator("GET", "/me/circles")).get("circles", [])
    out = []
    for circle in circles:
        models = (await _coordinator("GET", f"/me/circles/{circle['id']}/availability")).get("models", [])
        out.append({**circle, "models": models})
    return {"schemaVersion": 1, "circles": out}


@router.post("", status_code=201)
async def create_circle(body: CreateBody) -> dict:
    return await _coordinator("POST", "/me/circles", {"name": body.name.strip()})


@router.post("/invites/preview")
async def preview_invite(body: CodeBody) -> dict:
    return await _coordinator("POST", "/circles/invites/preview", {"code": body.code.strip()})


@router.post("/invites/accept")
async def accept_invite(body: CodeBody) -> dict:
    return await _coordinator("POST", "/circles/invites/accept", {"code": body.code.strip()})


@router.post("/{circle}/invites", status_code=201)
async def create_invite(circle: str, body: InviteBody | None = None) -> dict:
    body = body or InviteBody()
    return await _coordinator("POST", f"/me/circles/{await _circle_id(circle)}/invites", body.model_dump())


@router.delete("/{circle}/membership", status_code=204)
async def leave_circle(circle: str) -> Response:
    await _coordinator("DELETE", f"/me/circles/{await _circle_id(circle)}/membership")
    return Response(status_code=204)


@router.delete("/{circle}", status_code=204)
async def delete_circle(circle: str) -> Response:
    await _coordinator("DELETE", f"/me/circles/{await _circle_id(circle)}")
    return Response(status_code=204)
