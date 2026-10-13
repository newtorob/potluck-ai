"""Load-time model integrity: which sha256 do we trust as the reference?

The opt-in POTLUCK_VERIFY_MODEL_ON_LOAD re-check exists to catch a tampered
model.gguf. Its whole value hinges on the reference hash coming from a source the
tamperer cannot also rewrite. The catalog (models.json) ships read-only inside
the code-signed app bundle; the model.gguf.sha256 sidecar lives in the
user-writable ~/.potluck/models dir. So the reference must prefer the catalog and
treat the sidecar as a corruption-only fallback for non-catalog models.

These lock _load_verify_reference so a refactor can't silently start trusting the
attacker-writable sidecar over the bundled catalog hash.
"""
from __future__ import annotations

from potluck_local.routers.models import _load_verify_reference

CATALOG = "9c9f56a391a3abbd5b89d0245bf6106081bcc3173119d4229235dd9d23253f94"
TAMPERED = "dead" * 16  # 64 hex chars, what an attacker would write into the sidecar


def _write_sidecar(model_dir, value: str) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.gguf.sha256").write_text(value)


def test_prefers_catalog_over_sidecar(tmp_path):
    # The security case: a catalog model whose on-disk sidecar has been rewritten
    # to a tampered hash. We must still check against the bundled catalog hash.
    _write_sidecar(tmp_path, TAMPERED)
    manifest = {"slug": "qwen", "sha256": CATALOG}
    expected, source = _load_verify_reference("qwen", manifest, tmp_path)
    assert source == "catalog"
    assert expected == CATALOG


def test_catalog_hash_is_lowercased(tmp_path):
    manifest = {"slug": "qwen", "sha256": CATALOG.upper()}
    expected, source = _load_verify_reference("qwen", manifest, tmp_path)
    assert source == "catalog"
    assert expected == CATALOG  # lowercased for the ==-compare against hexdigest()


def test_sidecar_fallback_for_non_catalog_model(tmp_path):
    # No manifest (a model the user side-loaded): the sidecar is the best we have,
    # and it still catches accidental corruption.
    _write_sidecar(tmp_path, CATALOG)
    expected, source = _load_verify_reference("sideloaded", None, tmp_path)
    assert source == "sidecar"
    assert expected == CATALOG


def test_sidecar_fallback_when_catalog_hash_blank(tmp_path):
    # Catalog entry exists but carries no hash (should never happen for shipped
    # models, but don't crash) -> fall back to the sidecar.
    _write_sidecar(tmp_path, CATALOG)
    manifest = {"slug": "qwen", "sha256": ""}
    expected, source = _load_verify_reference("qwen", manifest, tmp_path)
    assert source == "sidecar"
    assert expected == CATALOG


def test_no_reference_available(tmp_path):
    # No manifest and no sidecar file -> ("", "none"); caller logs skip, doesn't raise.
    expected, source = _load_verify_reference("ghost", None, tmp_path)
    assert source == "none"
    assert expected == ""


def test_blank_sidecar_file_is_not_a_reference(tmp_path):
    _write_sidecar(tmp_path, "   \n")
    expected, source = _load_verify_reference("sideloaded", None, tmp_path)
    assert source == "none"
    assert expected == ""
