"""The privacy lint must actually read the files it claims to check.

A repository root resolved one level short would find none of its
targets, skip them silently and report a pass on every commit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

LINT = Path(__file__).resolve().parents[1] / "dev" / "lint_privacy.py"


def load():
    spec = importlib.util.spec_from_file_location("lint_privacy", LINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_scan_target_exists():
    lint = load()
    assert (lint.REPO_ROOT / "services" / "coordinator" / "coordinator").is_dir()
    assert (lint.REPO_ROOT / "apps" / "desktop" / "python" / "potluck_local" / "contributor").is_dir()
    for relpath in list(lint.AGGREGATE_RESPONSE_FILES) + list(lint.PEER_RELAY_FILES):
        assert (lint.REPO_ROOT / relpath).is_file(), relpath


def test_the_tree_passes():
    assert load().main() == 0


def test_a_planted_leak_is_seen(tmp_path, monkeypatch):
    lint = load()
    root = tmp_path / "services" / "coordinator" / "coordinator"
    root.mkdir(parents=True)
    (root / "leak.py").write_text(
        'async def f(conn, member_id):\n    await conn.ws.send_json({"member_id": member_id})\n'
    )
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    assert lint.lint_peer_bound(), "a peer-bound member_id was not flagged"


def test_a_missing_target_fails(tmp_path, monkeypatch):
    lint = load()
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    assert lint.lint_peer_bound()
    assert lint.lint_aggregate_responses()
    assert lint.lint_contributor()
    assert lint.lint_peer_relay()
    assert lint.main() != 0
