"""Every shipped catalog download must be pinned to a Hugging Face commit.

`resolve/main` lets a repo owner swap the file after we recorded its sha256.
A full 40-hex revision makes every install reproducible. To add or refresh a
model, run `make check-models ARGS=--write`, which emits pinned URLs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from potluck_local.services import model_registry

_PINNED = re.compile(r"^https://huggingface\.co/[^/]+/[^/]+/resolve/[0-9a-f]{40}/[^/?#][^?#]*\.gguf$")
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CATALOGS = [
    Path(__file__).resolve().parents[1] / "potluck_local" / "data" / "models.json",
    _REPO_ROOT / "services" / "api" / "potluck_api" / "data" / "models.json",
]


@pytest.mark.parametrize("path", _CATALOGS, ids=lambda p: p.parts[-4])
def test_catalog_urls_are_pinned_to_a_commit(path: Path) -> None:
    models = json.loads(path.read_text(encoding="utf-8"))["models"]
    assert models
    unpinned = [m["slug"] for m in models if not _PINNED.match(m["download_url"])]
    assert not unpinned, f"download_url must use resolve/<40-hex commit>/, not main or a short ref: {unpinned}"


def test_cloud_and_bundled_catalogs_agree_on_urls() -> None:
    urls = [
        {m["slug"]: (m["download_url"], m["sha256"], m["size_bytes"]) for m in json.loads(p.read_text())["models"]}
        for p in _CATALOGS
    ]
    assert urls[0] == urls[1]


def test_registry_serves_pinned_urls() -> None:
    assert all(_PINNED.match(m["download_url"]) for m in model_registry._load_bundled_catalog()["models"])
