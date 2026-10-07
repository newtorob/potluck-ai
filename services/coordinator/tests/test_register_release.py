"""The release registration tool: what production needs before a shipped
build's machines can attest."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from coordinator.models.release import ContributorRelease
from coordinator.tools.register_release import parse, register


def _hash() -> str:
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:64]


@pytest.mark.asyncio
async def test_registers_once_and_leaves_an_existing_row_alone(session_factory) -> None:
    h = _hash()
    try:
        async with session_factory.factory() as db:
            assert await register(db, binary_hash=h, version="0.1.9", signer="SHA256:abc", notes="mac") is True
            await db.commit()
        async with session_factory.factory() as db:
            assert await register(db, binary_hash=h, version="9.9.9", signer="SHA256:other", notes="again") is False
            await db.commit()
            row = (await db.execute(select(ContributorRelease).where(ContributorRelease.binary_hash == h))).scalar_one()
            assert (row.version, row.signer_key, row.notes) == ("0.1.9", "SHA256:abc", "mac")
    finally:
        async with session_factory.factory() as db:
            await db.execute(delete(ContributorRelease).where(ContributorRelease.binary_hash == h))
            await db.commit()


@pytest.mark.asyncio
async def test_a_registered_hash_passes_the_strict_release_gate(session_factory) -> None:
    """The row this tool writes is the row attestation looks for."""
    h = _hash()
    try:
        async with session_factory.factory() as db:
            await register(db, binary_hash=h, version="0.1.9", signer="SHA256:abc", notes=None)
            await db.commit()
        async with session_factory.factory() as db:
            assert await db.get(ContributorRelease, h) is not None
            assert await db.get(ContributorRelease, _hash()) is None
    finally:
        async with session_factory.factory() as db:
            await db.execute(delete(ContributorRelease).where(ContributorRelease.binary_hash == h))
            await db.commit()


@pytest.mark.parametrize("argv", [
    ["--hash", "unreleased-dev", "--version", "0.1.9", "--signer", "SHA256:abc"],
    ["--hash", "A" * 64, "--version", "0.1.9", "--signer", "SHA256:abc"],
    ["--hash", "a" * 64, "--version", "0.1.9'; --", "--signer", "SHA256:abc"],
    ["--hash", "a" * 64, "--version", "0.1.9"],
    ["--hash", "a" * 64, "--version", "0.1.9", "--signer", "has space"],
])
def test_refuses_anything_that_is_not_a_release(argv) -> None:
    with pytest.raises(SystemExit):
        parse(argv)


def test_accepts_a_release_and_a_listing() -> None:
    args = parse(["--hash", "a" * 64, "--version", "0.1.9", "--signer", "SHA256:AbC+/=", "--notes", "desktop mac arm64"])
    assert args.binary_hash == "a" * 64 and args.notes == "desktop mac arm64"
    assert parse(["--list"]).list is True
