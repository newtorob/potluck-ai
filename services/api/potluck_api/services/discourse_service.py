"""Discourse SSO (DiscourseConnect) integration.

Task: 10

Responsibilities:
- build_login_redirect(sso, sig, member) -> str
  1. HMAC-SHA256 verify sig against DISCOURSE_SSO_SECRET (constant-time)
  2. Base64-decode and parse the nonce + return_sso_url from the payload
  3. Build the response payload: nonce, external_id, email, username, name, admin
  4. Sign the response payload with DISCOURSE_SSO_SECRET
  5. Return the return_sso_url with ?sso=...&sig=... appended

The signature is computed over the base64 payload STRING in both directions,
which is what DiscourseConnect specifies.

external_id is the Potluck member.id: a stable, unique identifier so a forum
account binds permanently to the Potluck account (email/username can change).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import urllib.parse

from potluck_api.config import get_settings
from potluck_api.models.member import Member

# Discourse usernames: letters, numbers, and _ . - only.
_USERNAME_STRIP = re.compile(r"[^A-Za-z0-9_.-]")


class DiscourseConnectError(ValueError):
    """Raised when the inbound SSO payload is malformed or fails verification.

    Callers map this to HTTP 400 — the message is safe to surface.
    """


class DiscourseService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _sign(self, payload_b64: bytes) -> str:
        """HMAC-SHA256 hex digest of the base64 payload, per DiscourseConnect."""
        return hmac.new(
            self.settings.discourse_sso_secret.encode(),
            payload_b64,
            hashlib.sha256,
        ).hexdigest()

    def build_login_redirect(self, sso: str, sig: str, member: Member) -> str:
        """Verify the inbound SSO request and return the signed redirect URL."""
        # 1. Verify the inbound signature over the raw base64 sso string.
        expected = self._sign(sso.encode())
        if not hmac.compare_digest(expected, sig):
            raise DiscourseConnectError("Invalid SSO signature")

        # 2. Decode the payload and pull the nonce + return URL.
        try:
            decoded = base64.b64decode(sso, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise DiscourseConnectError("Malformed SSO payload") from exc

        params = dict(urllib.parse.parse_qsl(decoded))
        nonce = params.get("nonce")
        if not nonce:
            raise DiscourseConnectError("Missing nonce in SSO payload")

        return_url = params.get("return_sso_url") or (
            f"{self.settings.discourse_url.rstrip('/')}/session/sso_login"
        )

        # 3. Build the response payload bound to this member.
        response_params = {
            "nonce": nonce,
            "email": member.email,
            "external_id": str(member.id),
            "username": self._username(member),
            "name": member.display_name or "",
            "admin": "true" if member.is_admin else "false",
            # Skip Discourse's welcome PM; members already onboarded via the app.
            "suppress_welcome_message": "true",
        }

        # 4. Base64-encode and sign the response payload.
        response_b64 = base64.b64encode(urllib.parse.urlencode(response_params).encode())
        response_sig = self._sign(response_b64)

        # 5. Append sso + sig to the return URL.
        query = urllib.parse.urlencode({"sso": response_b64.decode(), "sig": response_sig})
        separator = "&" if "?" in return_url else "?"
        return f"{return_url}{separator}{query}"

    @staticmethod
    def _username(member: Member) -> str:
        """Derive a Discourse-safe username from the display name or email.

        Discourse re-validates and de-duplicates usernames on its side, so this
        only needs to produce a reasonable, character-set-valid seed.
        """
        raw = member.display_name or member.email.split("@", 1)[0]
        cleaned = _USERNAME_STRIP.sub("", raw).strip("_.-")
        return (cleaned or "member")[:20]
