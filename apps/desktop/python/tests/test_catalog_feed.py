"""Catalog feed: the cloud model-catalog feed must never compromise the
offline floor. These tests pin the guarantees the sidecar relies on:

  1. With no cached remote feed (offline / never-fetched / not-yet-deployed),
     the registry serves the bundled catalog and never returns empty.
  2. A cached remote feed is preferred only when it is schema-compatible and
     at least as fresh (updated_at) as the bundled catalog.
  3. A refresh against an unreachable/erroring endpoint returns False and
     never raises, leaving whatever the cache already had.
  4. list_models() annotates a newer entry with upgrade_from listing the
     installed older slugs it supersedes (the in-app "update available" wire).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from potluck_local.services import catalog_feed, model_registry


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    # The registry memoizes the merged catalog in a module global; reset it
    # around every test so each starts from a clean bundled-vs-cache decision.
    model_registry.invalidate_cache()
    yield
    model_registry.invalidate_cache()


def _point_cache_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(catalog_feed, "catalog_cache_path", lambda: path)


def test_load_catalog_falls_back_to_bundled_when_no_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _point_cache_at(monkeypatch, tmp_path / "does_not_exist.json")
    models = model_registry._load_catalog()
    assert len(models) >= 1
    assert all("slug" in m for m in models)


@pytest.mark.parametrize('same_hash,explicit_tags,expected', [
    (True, None, ['writing']), (False, None, None), (True, [], []), (True, ['coding'], ['coding']),
])
def test_task_guidance_survives_older_feeds_only_for_identical_weights(monkeypatch, same_hash, explicit_tags, expected):
    original = {'slug': 'model', 'sha256': 'a' * 64, 'use_cases': ['writing']}
    remote = {'slug': 'model', 'sha256': ('a' if same_hash else 'b') * 64}
    if explicit_tags is not None:
        remote['use_cases'] = explicit_tags
    monkeypatch.setattr(model_registry, '_load_bundled_catalog', lambda: {'schema_version': 1, 'models': [original]})
    monkeypatch.setattr(catalog_feed, 'read_cached_catalog', lambda: {'schema_version': 1, 'models': [remote]})
    assert model_registry._load_catalog()[0].get('use_cases') == expected
    assert original['use_cases'] == ['writing']


def test_cached_remote_wins_when_fresher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at": "2999-01-01T00:00:00Z",
                "models": [
                    {
                        "slug": "future-model-9b",
                        "name": "Future Model 9B",
                        "download_url": "https://huggingface.co/x/y/resolve/0123456789abcdef0123456789abcdef01234567/z.gguf",
                        "sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _point_cache_at(monkeypatch, cache)
    models = model_registry._load_catalog()
    assert [m["slug"] for m in models] == ["future-model-9b"]


def test_invalid_cache_is_ignored_and_bundled_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "cache.json"
    # missing required per-entry fields -> _validate rejects -> bundled floor
    cache.write_text(json.dumps({"schema_version": 2, "models": [{"slug": "x"}]}), encoding="utf-8")
    _point_cache_at(monkeypatch, cache)
    models = model_registry._load_catalog()
    assert len(models) > 1  # the real bundled catalog, not the 1-entry junk


def test_refresh_once_offline_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def _boom(_url: str):
        raise RuntimeError("offline")

    monkeypatch.setattr(catalog_feed, "_fetch", _boom)
    # no raise, no cache write when the endpoint is unreachable / not deployed
    assert asyncio.run(catalog_feed.refresh_once()) is False


def test_upgrade_from_flags_installed_predecessor(monkeypatch: pytest.MonkeyPatch) -> None:
    # qwen3-4b-instruct-2507-q4 supersedes qwen-2.5-3b-instruct-q4 in the catalog.
    monkeypatch.setattr(
        model_registry, "_installed_slugs", lambda _base: {"qwen-2.5-3b-instruct-q4"}
    )

    class _NoProgress:
        def get_progress(self, _slug):
            return None

    monkeypatch.setattr(
        "potluck_local.services.model_downloader.get_downloader", lambda: _NoProgress()
    )
    flagged = {m["slug"]: m.get("upgrade_from") for m in model_registry.list_models() if m.get("upgrade_from")}
    assert flagged.get("qwen3-4b-instruct-2507-q4") == ["qwen-2.5-3b-instruct-q4"]


def test_no_upgrade_from_when_nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_registry, "_installed_slugs", lambda _base: set())

    class _NoProgress:
        def get_progress(self, _slug):
            return None

    monkeypatch.setattr(
        "potluck_local.services.model_downloader.get_downloader", lambda: _NoProgress()
    )
    assert all("upgrade_from" not in m for m in model_registry.list_models())


def test_feed_with_unpinned_download_url_is_rejected() -> None:
    def feed(url: str) -> dict:
        return {"schema_version": 2, "models": [{"slug": "m", "download_url": url, "sha256": "0" * 64}]}

    pinned = "https://huggingface.co/x/y/resolve/" + "a" * 40 + "/z.gguf"
    assert catalog_feed._validate(feed(pinned))
    for bad in ("main", "v1.0", "a" * 39, "A" * 40):
        assert not catalog_feed._validate(feed(f"https://huggingface.co/x/y/resolve/{bad}/z.gguf"))
