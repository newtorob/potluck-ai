"""Register a shipped contributor build so its machines may attest.

With ``REQUIRE_REGISTERED_RELEASE=true`` the coordinator refuses a peer whose
``binary_hash`` is not in ``contributor_releases``. Every public desktop and
CLI build therefore needs one row, holding the hash the build stamped into
its engine bundle (``contributor-release.json``; see
``scripts/dev/sign-contributor-release.py emit-sql-stamp``).

Run where the coordinator runs, so it uses the service's own database
settings and nothing is copied out of production::

    fly ssh console -a potluck-coordinator -C \\
      "python -m coordinator.tools.register_release --hash <sha256> \\
       --version 0.1.9 --signer <release-key fingerprint> --notes 'desktop mac arm64'"

Safe to repeat: a hash that is already registered is left as it is. ``--list``
prints the registered releases, newest first.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from coordinator.db.database import close_db, get_session_factory, init_db
from coordinator.models.release import ContributorRelease

_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9A-Za-z.+_-]{1,32}")
_SIGNER = re.compile(r"[0-9A-Za-z:+/=_-]{1,128}")


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="register_release", description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print registered releases and exit")
    parser.add_argument("--hash", dest="binary_hash", help="the stamped SHA-256 (lowercase hex)")
    parser.add_argument("--version")
    parser.add_argument("--signer", help="fingerprint of the release key that signed the hash")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args(argv)
    if args.list:
        return args
    if not (args.binary_hash and args.version and args.signer):
        parser.error("--hash, --version and --signer are required")
    if not _SHA256.fullmatch(args.binary_hash):
        parser.error("--hash must be 64 lowercase hex characters")
    if not _VERSION.fullmatch(args.version):
        parser.error("--version is not a version")
    if not _SIGNER.fullmatch(args.signer):
        parser.error("--signer is not a key fingerprint")
    if args.notes is not None and len(args.notes) > 500:
        parser.error("--notes is too long")
    return args


async def register(db, *, binary_hash: str, version: str, signer: str, notes: str | None) -> bool:
    """Insert the release unless its hash is already registered. Returns
    whether a row was added."""
    result = await db.execute(
        insert(ContributorRelease)
        .values(binary_hash=binary_hash, version=version, signer_key=signer, notes=notes)
        .on_conflict_do_nothing(index_elements=["binary_hash"])
        .returning(ContributorRelease.binary_hash)
    )
    return result.scalar_one_or_none() is not None


async def _run(args: argparse.Namespace) -> int:
    await init_db()
    try:
        async with get_session_factory()() as db:
            if not args.list:
                added = await register(db, binary_hash=args.binary_hash, version=args.version,
                                       signer=args.signer, notes=args.notes)
                await db.commit()
                print(("registered " if added else "already registered ") + args.binary_hash)
            rows = (await db.execute(
                select(ContributorRelease).order_by(ContributorRelease.signed_at.desc()).limit(20)
            )).scalars().all()
            for row in rows:
                print(f"{row.signed_at:%Y-%m-%d %H:%M}  {row.version:<10} {row.binary_hash}  {row.notes or ''}")
    finally:
        await close_db()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(parse(argv)))


if __name__ == "__main__":
    sys.exit(main())
