"""Suite-wide isolation.

The engine defaults to the public coordinator and reads the member's sign-in
from the data directory. A unit test must never do either with the real ones,
so every test starts with an unroutable coordinator and an empty data
directory. Tests that need something else set it themselves.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_engine_environment(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("POTLUCK_COORDINATOR_HTTP_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path_factory.mktemp("potluck-data")))
