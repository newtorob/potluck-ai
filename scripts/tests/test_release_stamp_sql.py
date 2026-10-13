"""emit-sql-stamp registers the hash a shipped engine presents.

The engine attests with the digest stamped into contributor-release.json at
build time. emit-sql hashes one file, so its row would never match a
real build and production (which requires a registered release) would
refuse every shipped app.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "dev" / "sign-contributor-release.py"
HASH = "ab" * 32


def run(*args: str, key_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--key-dir", str(key_dir), *args],
                          capture_output=True, text=True)


def test_emits_the_stamped_hash_and_version(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    assert run("keygen", key_dir=keys).returncode == 0
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "contributor-release.json").write_text(json.dumps(
        {"binary_hash": HASH, "version": "0.1.9", "built_at": "2026-10-07T00:00:00+00:00"}))
    (bundle / "engine").write_bytes(b"signed after the stamp, so its bytes no longer match")

    for target in (bundle, bundle / "contributor-release.json"):
        out = run("emit-sql-stamp", str(target), "--notes", "it's the mac build", key_dir=keys)
        assert out.returncode == 0, out.stderr
        assert f"('{HASH}', '0.1.9', now()," in out.stdout
        assert "'it''s the mac build'" in out.stdout
        assert "ON CONFLICT (binary_hash) DO NOTHING;" in out.stdout
        assert "PRIVATE" not in out.stdout


def test_refuses_a_dev_build_and_a_missing_stamp(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    assert run("keygen", key_dir=keys).returncode == 0
    stamp = tmp_path / "contributor-release.json"
    stamp.write_text(json.dumps({"binary_hash": "unreleased-dev", "version": "0.0.0"}))
    assert run("emit-sql-stamp", str(stamp), key_dir=keys).returncode == 2
    assert run("emit-sql-stamp", str(tmp_path / "nope.json"), key_dir=keys).returncode == 2
    stamp.write_text(json.dumps({"binary_hash": HASH, "version": "0.1.9'; DROP TABLE x; --"}))
    assert run("emit-sql-stamp", str(stamp), key_dir=keys).returncode == 2
