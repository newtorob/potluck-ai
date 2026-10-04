"""Authenticated, loopback-only batch management. Inputs are supplied, never paths."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from potluck_local.services.batch_jobs import (
    BatchManager, MAX_FILES, MAX_FILE_BYTES, MAX_INPUT_BYTES, summary,
)

router = APIRouter()


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelPin(StrictBody):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Document(StrictBody):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=MAX_FILE_BYTES)

    @field_validator("name")
    @classmethod
    def label(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in value):
            raise ValueError("Use a printable file name, not a path")
        return value

    @field_validator("text")
    @classmethod
    def content(cls, value):
        if not value.strip() or "\x00" in value or len(value.encode("utf8")) > MAX_FILE_BYTES:
            raise ValueError("Supply nonempty UTF-8 text up to 16 KiB")
        return value


class Resume(StrictBody):
    model: ModelPin
    scope: Literal["local", "mine"]


class Submit(Resume):
    id: UUID
    files: list[Document] = Field(min_length=1, max_length=MAX_FILES)
    concurrency: int = Field(default=3, ge=1, le=8, strict=True)
    max_tokens: int = Field(default=512, ge=64, le=2048, strict=True)

    @model_validator(mode="after")
    def input_limit(self):
        if sum(len(f.text.encode("utf8")) for f in self.files) > MAX_INPUT_BYTES:
            raise ValueError("Combined input must not exceed 256 KiB")
        return self


def manager(request: Request) -> BatchManager:
    value = getattr(request.app.state, "batch_manager", None)
    if value is None:
        raise HTTPException(503, "Batch jobs unavailable. Start or upgrade the local runtime.")
    return value


@router.post("", status_code=202)
async def submit(body: Submit, request: Request):
    return manager(request).submit(body.model_dump(mode="json"))


@router.get("")
async def listing(request: Request):
    return {"schemaVersion": 1, "jobs": [summary(j, details=False) for j in reversed(manager(request).store.all())]}


@router.get("/{job_id}")
async def status(job_id: UUID, request: Request):
    return summary(manager(request).store.get(str(job_id)))


@router.post("/{job_id}/cancel")
async def cancel(job_id: UUID, request: Request):
    return await manager(request).cancel(str(job_id))


@router.post("/{job_id}/resume")
async def resume(job_id: UUID, body: Resume, request: Request):
    return manager(request).resume(str(job_id), body.model.model_dump(), body.scope)


@router.get("/{job_id}/results/{item_id}")
async def result(job_id: UUID, item_id: str, request: Request):
    job = manager(request).store.get(str(job_id))
    item = next((i for i in job["items"] if i["id"] == item_id), None)
    if item is None:
        raise HTTPException(404, "Batch piece not found.")
    return {"schemaVersion": 1, "id": item["id"], "name": item["name"], "state": item["state"],
            "inputSha256": item["inputSha256"], "text": item["output"] if item["state"] == "completed" else None}


@router.delete("/{job_id}")
async def delete(job_id: UUID, request: Request):
    return manager(request).delete(str(job_id))
