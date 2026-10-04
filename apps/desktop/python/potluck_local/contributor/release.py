"""Which build is this contributor? The ``binary_hash`` the coordinator sees.

Resolution order (first hit wins):
  1. ``POTLUCK_BINARY_HASH`` in the environment (bench fleet, tests).
  2. ``contributor-release.json`` next to the frozen sidecar executable,
     written by ``build.py`` after PyInstaller finishes.
  3. ``unreleased-dev`` for a source checkout with neither.

The coordinator treats an unknown hash as advisory unless its
``require_registered_release`` flag is on, so a dev build can still join a
dev coordinator; the hash it sent is recorded on its heartbeat row either
way.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RELEASE_FILE = "contributor-release.json"
DEV_HASH = "unreleased-dev"


def compute_bundle_hash(bundle_dir: Path) -> str:
    """SHA-256 over the bundle's contents: every regular file, sorted by its
    POSIX-style relative path, contributing ``path NUL sha256 LF``. The
    release file itself is skipped so writing it does not change the hash.
    Deterministic across machines for byte-identical bundles."""
    bundle_dir = Path(bundle_dir)
    digest = hashlib.sha256()
    for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(bundle_dir).as_posix()
        if rel == RELEASE_FILE:
            continue
        file_hash = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                file_hash.update(chunk)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_release_info(bundle_dir: Path, version: str) -> dict[str, str]:
    """Compute the bundle hash and write ``contributor-release.json`` into
    the bundle. Returns what was written."""
    bundle_dir = Path(bundle_dir)
    info = {
        "binary_hash": compute_bundle_hash(bundle_dir),
        "version": version,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (bundle_dir / RELEASE_FILE).write_text(json.dumps(info, indent=2) + "\n")
    return info


def load_release_info(bundle_dir: Path | None) -> dict[str, str] | None:
    """Read the release file if the bundle has one; None otherwise (missing,
    unreadable, or malformed all count as absent)."""
    if bundle_dir is None:
        return None
    path = Path(bundle_dir) / RELEASE_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("binary_hash"), str):
        return None
    return data


def frozen_bundle_dir() -> Path | None:
    """The directory of the running PyInstaller executable, or None when
    running from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def resolve_binary_hash(
    env_value: str | None,
    bundle_dir: Path | None = None,
) -> tuple[str, str]:
    """Return ``(binary_hash, source)`` where source is one of ``env``,
    ``release_file``, ``dev``."""
    if env_value and env_value.strip():
        return env_value.strip(), "env"
    info = load_release_info(bundle_dir)
    if info is not None:
        return info["binary_hash"], "release_file"
    return DEV_HASH, "dev"


def main(argv: list[str] | None = None) -> int:
    """``python -m potluck_local.contributor.release write <bundle_dir> --version X``
    is what build.py calls; ``show <bundle_dir>`` prints the current file."""
    import argparse

    parser = argparse.ArgumentParser(prog="potluck-contributor-release")
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="hash the bundle and write contributor-release.json")
    w.add_argument("bundle_dir")
    w.add_argument("--version", required=True)
    s = sub.add_parser("show", help="print contributor-release.json")
    s.add_argument("bundle_dir")
    args = parser.parse_args(argv)
    if args.cmd == "write":
        info = write_release_info(Path(args.bundle_dir), args.version)
        print(json.dumps(info))
        return 0
    info = load_release_info(Path(args.bundle_dir))
    print(json.dumps(info) if info else "null")
    return 0 if info else 1


if __name__ == "__main__":
    raise SystemExit(main())
