"""Discourse SSO (DiscourseConnect) integration.

Task: 10

Responsibilities:
- handle_sso(sso_payload, sig, member) -> RedirectResponse
  1. HMAC-SHA256 verify sig against DISCOURSE_SSO_SECRET
  2. Base64-decode and parse the nonce from sso_payload
  3. Build response payload: nonce, external_id, email, username, name, admin
  4. Sign response payload with DISCOURSE_SSO_SECRET
  5. Redirect to DISCOURSE_URL/session/sso_login?sso=...&sig=...
- sync_discourse_user(member) -> None  — push profile updates via Discourse API
"""
from __future__ import annotations


class DiscourseService:
    pass
