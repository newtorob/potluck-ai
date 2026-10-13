"""The privacy lint must actually read the files it claims to check.

A repository root resolved one level short would find none of its
targets, skip them silently and report a pass on every commit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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
    for relpath in (
        list(lint.AGGREGATE_RESPONSE_FILES)
        + list(lint.CONTRIBUTOR_SERVICE_FILES)
        + list(lint.PEER_RELAY_FILES)
    ):
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


SERVICES = "apps/desktop/python/potluck_local/services/"

# Each file that sits outside the tree its rule is named for, with the lint
# function that must read it, the label that function puts on a finding and a
# leak that function must flag in it.
NEW_TARGETS = [
    (
        SERVICES + "contributor_service.py",
        "lint_contributor",
        "contributor-leak",
        'import logging\nlogger = logging.getLogger("x")\n\n'
        'def serve(job, prompt):\n    logger.info("job %s", prompt)\n',
    ),
    (
        SERVICES + "remote_stream.py",
        "lint_peer_relay",
        "relay-leak",
        'import logging\nlogger = logging.getLogger("x")\n\n'
        'def parse(event, content):\n    logger.warning("bad event %s", content)\n',
    ),
    (
        SERVICES + "batch_jobs.py",
        "lint_peer_relay",
        "relay-leak",
        'import json\n\ndef keep(job, messages, f):\n    json.dump(messages, f)\n',
    ),
    (
        SERVICES + "split_manager.py",
        "lint_peer_relay",
        "relay-leak",
        'import logging\nlogger = logging.getLogger("x")\n\n'
        'def forward(data):\n    logger.debug("frame %r", data)\n',
    ),
]
TARGET_IDS = [target[0].rsplit("/", 1)[1] for target in NEW_TARGETS]
CLEAN = "def ok(job_id):\n    return job_id\n"


def build_tree(lint, root, *, skip=None):
    """A repository root holding every file the lints list, all clean."""
    (root / "services" / "coordinator" / "coordinator").mkdir(parents=True, exist_ok=True)
    contributor = root / "apps" / "desktop" / "python" / "potluck_local" / "contributor"
    contributor.mkdir(parents=True, exist_ok=True)
    (contributor / "client.py").write_text(CLEAN)
    listed = (
        list(lint.AGGREGATE_RESPONSE_FILES)
        + list(lint.CONTRIBUTOR_SERVICE_FILES)
        + list(lint.PEER_RELAY_FILES)
    )
    for relpath in listed:
        if relpath == skip:
            continue
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CLEAN)


def test_the_new_targets_are_listed():
    lint = load()
    assert SERVICES + "contributor_service.py" in lint.CONTRIBUTOR_SERVICE_FILES
    for name in ("remote_stream.py", "batch_jobs.py", "split_manager.py"):
        assert SERVICES + name in lint.PEER_RELAY_FILES, name
    assert SERVICES + "managed_agents.py" not in lint.PEER_RELAY_FILES
    assert SERVICES + "managed_agents.py" not in lint.CONTRIBUTOR_SERVICE_FILES


@pytest.mark.parametrize("relpath,function,label,leak", NEW_TARGETS, ids=TARGET_IDS)
def test_a_planted_leak_in_a_new_target_is_seen(tmp_path, monkeypatch, relpath, function, label, leak):
    lint = load()
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    build_tree(lint, tmp_path)
    assert getattr(lint, function)() == [], "the clean tree must not produce findings"
    (tmp_path / relpath).write_text(leak)
    findings = getattr(lint, function)()
    assert any(f.startswith(relpath + ":") and f": {label}:" in f for f in findings), findings


@pytest.mark.parametrize("relpath,function,label,leak", NEW_TARGETS, ids=TARGET_IDS)
def test_a_missing_new_target_fails(tmp_path, monkeypatch, relpath, function, label, leak):
    lint = load()
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    build_tree(lint, tmp_path, skip=relpath)
    findings = getattr(lint, function)()
    assert any(f.startswith(relpath + ": listed file is missing") for f in findings), findings


def test_the_split_frame_name_counts_as_content_only_in_the_split_manager(tmp_path, monkeypatch):
    lint = load()
    monkeypatch.setattr(lint, "REPO_ROOT", tmp_path)
    build_tree(lint, tmp_path)
    framed = 'import logging\nlogger = logging.getLogger("x")\n\ndef f(data):\n    logger.debug("n=%d", len(data))\n'
    unrelated = 'import logging\nlogger = logging.getLogger("x")\n\ndef f(data):\n    logger.debug("got %s", data)\n'
    (tmp_path / (SERVICES + "split_manager.py")).write_text(framed)
    (tmp_path / (SERVICES + "remote_stream.py")).write_text(unrelated)
    assert lint.lint_peer_relay() == []
    (tmp_path / (SERVICES + "split_manager.py")).write_text(unrelated)
    findings = lint.lint_peer_relay()
    assert [f for f in findings if f.startswith(SERVICES + "split_manager.py:")], findings
