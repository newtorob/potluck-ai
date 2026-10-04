"""Regression test — 2026-07-31 P1 security audit, finding #1.

DELETE /models/{slug} built `models_dir() / slug` and shutil.rmtree'd it with
NO validation. A slug of ".." (which is exactly what a URL-encoded "%2e%2e"
path segment decodes to before FastAPI/Starlette ever hand the string to the
route function) resolves to models_dir()'s PARENT — the whole ~/.potluck data
directory — and shutil.rmtree deletes it. The auditor reproduced this getting
a 200 response while their whole ~/.potluck directory vanished.

The fix (_safe_model_dir in routers/models.py) rejects any slug that doesn't
resolve to a direct child of models_dir(), applied to every slug -> filesystem
path use in the router (delete, install, load).

Isolated via POTLUCK_DATA_DIR so this test can never touch a real ~/.potluck.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    from potluck_local.services import model_lifecycle

    # Earlier lifespan tests shut down their runtime. Each isolated model store
    # needs its own lifecycle so that process-wide shutdown state cannot turn
    # path-validation responses into unrelated 503 errors.
    monkeypatch.setattr(model_lifecycle, "_lifecycle", model_lifecycle.ModelLifecycle())
    data_dir = tmp_path / "potluck_data"
    models = data_dir / "models"
    models.mkdir(parents=True)
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(data_dir))

    # Sentinel content elsewhere in the data dir — the destructive bug
    # deletes models_dir()'s PARENT, i.e. everything here, not just models/.
    (data_dir / "config.json").write_text('{"sentinel": true}')
    installed = models / "real-model"
    installed.mkdir()
    (installed / "model.gguf").write_bytes(b"fake weights")

    return data_dir


@pytest.fixture()
def client():
    # peer_access_middleware only grants the full (loopback) surface to a
    # caller whose socket peer IP is actually 127.0.0.1/::1/localhost --
    # TestClient's default fake host ("testclient") is treated as a
    # non-loopback caller and gets 403'd before reaching the route at all.
    return TestClient(
        create_app(), client=("127.0.0.1", 50000), raise_server_exceptions=False
    )


def test_delete_percent_encoded_dotdot_slug_is_rejected(isolated_data_dir, client):
    """The auditor's exact repro: DELETE /models/%2e%2e must NOT 200, and
    ~/.potluck (here, the isolated data_dir) must survive completely intact."""
    resp = client.delete("/models/%2e%2e")

    assert resp.status_code != 200
    assert resp.status_code == 400

    # The whole data dir — not just models/ — must still exist with its
    # original contents. Pre-fix this whole tree is gone.
    assert isolated_data_dir.exists()
    assert (isolated_data_dir / "config.json").exists()
    assert (isolated_data_dir / "models" / "real-model" / "model.gguf").exists()


def test_delete_literal_dotdot_slug_is_rejected(isolated_data_dir, client):
    """Same bug class without relying on percent-decoding: a literal ".."
    path segment must never delete anything. httpx/Starlette's own URL-join
    normalisation collapses a literal (unencoded) ".." before it even
    reaches routing (-> 404, never reaches the handler at all) -- still a
    safe outcome, just via a different layer than the %2e%2e case above,
    which DOES reach the handler as a literal ".." slug and is what
    _safe_model_dir exists to reject (400)."""
    resp = client.delete("/models/..")
    assert resp.status_code in (400, 404)
    assert isolated_data_dir.exists()
    assert (isolated_data_dir / "config.json").exists()


def test_delete_nested_traversal_slug_is_rejected(isolated_data_dir, client):
    """A slug like "foo/../.." must not sneak past a naive ".." blocklist by
    resolving out through an intermediate segment."""
    resp = client.delete("/models/foo%2f..%2f..")
    assert resp.status_code in (400, 404)
    assert isolated_data_dir.exists()
    assert (isolated_data_dir / "config.json").exists()


def test_delete_valid_installed_slug_still_works(isolated_data_dir, client):
    """The fix must not collaterally break deleting a real, legitimately
    installed model."""
    resp = client.delete("/models/real-model")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert not (isolated_data_dir / "models" / "real-model").exists()
    # Nothing else in the data dir was touched.
    assert (isolated_data_dir / "config.json").exists()


def test_delete_unknown_but_safe_slug_is_404_not_400(isolated_data_dir, client):
    """A syntactically valid slug that just isn't installed is a 404 (not
    found), distinct from the 400 (invalid) a traversal attempt gets."""
    resp = client.delete("/models/never-installed")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Unit-level coverage of the shared helper, and of the OTHER slug -> path
# uses in the router the auditor asked to be checked for the same bug class.
# ---------------------------------------------------------------------------


def test_safe_model_dir_rejects_traversal_and_separators(isolated_data_dir):
    from potluck_local.routers.models import _safe_model_dir
    from fastapi import HTTPException

    for bad_slug in ("..", ".", "../etc", "a/../..", "a/b", "a\\b", ""):
        with pytest.raises(HTTPException) as exc_info:
            _safe_model_dir(bad_slug)
        assert exc_info.value.status_code == 400


def test_safe_model_dir_accepts_plain_slug(isolated_data_dir):
    from potluck_local.routers.models import _safe_model_dir
    from potluck_local.utils.paths import models_dir

    resolved = _safe_model_dir("qwen2.5-7b-instruct")
    assert resolved == (models_dir() / "qwen2.5-7b-instruct").resolve()
    assert resolved.parent == models_dir().resolve()


def test_load_model_rejects_traversal_slug(isolated_data_dir, client):
    """POST /models/{slug}/load must not build a filesystem path from an
    unvalidated slug either — same class of bug, different route."""
    resp = client.post("/models/%2e%2e/load")
    assert resp.status_code == 400
