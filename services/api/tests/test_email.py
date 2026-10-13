"""Log-hygiene helper: recipient addresses are masked before they reach logs."""
from __future__ import annotations

from potluck_api.services.email_service import _mask_email

# async so the async autouse fixtures in conftest apply cleanly; the bodies are sync.


async def test_mask_keeps_domain_hides_local():
    assert _mask_email("member@example.com") == "m***@example.com"


async def test_mask_handles_missing_at():
    assert _mask_email("not-an-email") == "<redacted>"


async def test_mask_handles_empty_local():
    assert _mask_email("@example.com") == "***@example.com"
