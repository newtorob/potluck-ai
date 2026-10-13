"""link_detect.detect_link_type() - contract: it always returns a known literal and
never raises, on any OS, with any (mis)behaving system tool.

This sits on the /peer/capabilities hot path, so it must be cheap and crash-proof: a
detection failure has to degrade to 'unknown' (a mild planner penalty), never propagate.
The module is deliberately torch-free, so these tests run in the base environment.
"""

from __future__ import annotations

import potluck_local.services.link_detect as link_detect
from potluck_local.services.link_detect import UNKNOWN, WIFI, WIRED, detect_link_type

_VALID = {WIRED, WIFI, UNKNOWN}


def _reset_cache() -> None:
    """Clear the process-lifetime cache so each test re-runs detection."""
    link_detect._link_cache = None


def test_detect_link_type_returns_valid_literal() -> None:
    """Real detection on this host returns one of the three known literals."""
    _reset_cache()
    result = detect_link_type()
    assert result in _VALID


def test_detect_link_type_is_cached() -> None:
    """Second call returns the cached value without re-running detection."""
    _reset_cache()
    first = detect_link_type()
    # Poison the underlying detector; if the cache works, it is never consulted.
    link_detect._link_cache = "wired"
    assert detect_link_type() == "wired"
    _reset_cache()
    assert first in _VALID


def test_detect_link_type_degrades_to_unknown_on_failure(monkeypatch) -> None:
    """If the uncached detector raises, detect_link_type swallows it and returns
    'unknown' - it must never propagate an exception onto the capabilities path."""
    _reset_cache()

    def _boom() -> str:
        raise RuntimeError("simulated detection failure")

    monkeypatch.setattr(link_detect, "_detect_uncached", _boom)
    assert detect_link_type() == UNKNOWN
    _reset_cache()
