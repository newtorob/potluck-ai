"""Tests for local file permissions (2026-07-17 security audit, MEDIUM).

~/.potluck/config.json holds the gateway API key in cleartext and
~/.potluck/data/memory.db holds the full content of every stored memory.
Neither should be world-readable. Skipped on Windows, where POSIX mode bits
don't apply the same way (os.chmod is best-effort there, per the guarded
try/except in the implementation).
"""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.settings_manager import PotluckSettings, save_settings

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits; chmod is best-effort on Windows"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------


def test_config_json_is_0600_after_save(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "config.json"
    save_settings(PotluckSettings(), cfg)
    assert _mode(cfg) == 0o600


def test_config_dir_is_0700_after_save(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "config.json"
    save_settings(PotluckSettings(), cfg)
    assert _mode(cfg.parent) == 0o700


def test_config_permissions_reapplied_idempotently(tmp_path: Path) -> None:
    """A world-readable file left by an older build gets locked down on the
    next save — the fix must self-heal existing installs, not just new ones."""
    cfg = tmp_path / "config.json"
    save_settings(PotluckSettings(), cfg)
    cfg.chmod(0o644)  # simulate a pre-fix world-readable file
    assert _mode(cfg) == 0o644

    save_settings(PotluckSettings(), cfg)
    assert _mode(cfg) == 0o600


# ---------------------------------------------------------------------------
# memory.db (+ WAL/SHM siblings) and its directories
# ---------------------------------------------------------------------------


def test_memory_db_is_0600_after_init(tmp_path: Path) -> None:
    db_path = tmp_path / "potluck" / "data" / "memory.db"
    _run(init_memory_db(db_path))
    assert _mode(db_path) == 0o600


def test_memory_db_dirs_are_0700_after_init(tmp_path: Path) -> None:
    db_path = tmp_path / "potluck" / "data" / "memory.db"
    _run(init_memory_db(db_path))
    assert _mode(db_path.parent) == 0o700          # data/
    assert _mode(db_path.parent.parent) == 0o700    # ~/.potluck


def test_memory_db_wal_shm_are_0600_if_present(tmp_path: Path) -> None:
    db_path = tmp_path / "potluck" / "data" / "memory.db"
    _run(init_memory_db(db_path))
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            assert _mode(sidecar) == 0o600


def test_memory_db_permissions_reapplied_idempotently(tmp_path: Path) -> None:
    """Re-running init_memory_db (every sidecar/MCP-server startup) re-locks
    down a file that somehow ended up world-readable."""
    db_path = tmp_path / "potluck" / "data" / "memory.db"
    _run(init_memory_db(db_path))
    db_path.chmod(0o644)
    assert _mode(db_path) == 0o644

    _run(init_memory_db(db_path))  # idempotent re-init
    assert _mode(db_path) == 0o600
