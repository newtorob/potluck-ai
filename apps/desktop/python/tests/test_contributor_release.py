"""binary_hash resolution and the build-time bundle hash."""
from __future__ import annotations

import json

from potluck_local.contributor import release


def _make_bundle(root):
    (root / "potluck-local").write_bytes(b"\x7fELF fake")
    (root / "_internal").mkdir()
    (root / "_internal" / "base_library.zip").write_bytes(b"zip" * 100)
    (root / "_internal" / "potluck_local" ).mkdir()
    (root / "_internal" / "potluck_local" / "app.pyc").write_bytes(b"pyc")
    return root


def test_bundle_hash_is_deterministic_and_ignores_release_file(tmp_path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    _make_bundle(bundle)
    first = release.compute_bundle_hash(bundle)
    assert len(first) == 64
    assert release.compute_bundle_hash(bundle) == first
    info = release.write_release_info(bundle, "0.1.5")
    assert info["binary_hash"] == first
    assert (bundle / release.RELEASE_FILE).exists()
    # Writing the release file must not move the hash.
    assert release.compute_bundle_hash(bundle) == first


def test_bundle_hash_changes_when_any_file_changes(tmp_path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    _make_bundle(bundle)
    before = release.compute_bundle_hash(bundle)
    (bundle / "_internal" / "potluck_local" / "app.pyc").write_bytes(b"pyc-changed")
    assert release.compute_bundle_hash(bundle) != before
    # Same content at a different relative path also changes it.
    (bundle / "_internal" / "potluck_local" / "app.pyc").write_bytes(b"pyc")
    (bundle / "_internal" / "extra.bin").write_bytes(b"")
    assert release.compute_bundle_hash(bundle) != before


def test_resolve_prefers_env_then_release_file_then_dev(tmp_path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    _make_bundle(bundle)
    assert release.resolve_binary_hash("abc123", bundle) == ("abc123", "env")
    assert release.resolve_binary_hash("  ", None) == (release.DEV_HASH, "dev")
    assert release.resolve_binary_hash(None, bundle) == (release.DEV_HASH, "dev")
    release.write_release_info(bundle, "0.1.5")
    got, source = release.resolve_binary_hash(None, bundle)
    assert source == "release_file" and got == release.compute_bundle_hash(bundle)


def test_malformed_release_file_counts_as_absent(tmp_path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / release.RELEASE_FILE).write_text("{not json")
    assert release.load_release_info(bundle) is None
    (bundle / release.RELEASE_FILE).write_text(json.dumps({"version": "x"}))
    assert release.load_release_info(bundle) is None
    assert release.resolve_binary_hash("", bundle) == (release.DEV_HASH, "dev")


def test_cli_write_and_show(tmp_path, capsys) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    _make_bundle(bundle)
    assert release.main(["write", str(bundle), "--version", "0.1.5"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["version"] == "0.1.5"
    assert release.main(["show", str(bundle)]) == 0
    assert json.loads(capsys.readouterr().out)["binary_hash"] == written["binary_hash"]
