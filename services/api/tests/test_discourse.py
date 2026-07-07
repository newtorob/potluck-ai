"""Discourse SSO (DiscourseConnect): auth gate, signature verification, and the
signed round-trip. HMAC is self-consistent with the dev secret, so no live
Discourse is needed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

from potluck_api.config import get_settings
from tests.factories import create_member, login


def _inbound(nonce: str = "nonce123", return_url: str = "https://forum.example/session/sso_login"):
    """Build a valid inbound (sso, sig) pair the way Discourse would."""
    secret = get_settings().discourse_sso_secret
    payload = urllib.parse.urlencode({"nonce": nonce, "return_sso_url": return_url})
    sso = base64.b64encode(payload.encode()).decode()
    sig = hmac.new(secret.encode(), sso.encode(), hashlib.sha256).hexdigest()
    return sso, sig


def _decode_redirect(location: str) -> tuple[dict, dict]:
    """Return (query params, decoded sso payload) from a redirect Location."""
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    payload = dict(urllib.parse.parse_qsl(base64.b64decode(query["sso"]).decode()))
    return query, payload


async def test_requires_auth(client):
    sso, sig = _inbound()
    r = await client.get("/auth/discourse-sso", params={"sso": sso, "sig": sig})
    assert r.status_code == 401


async def test_bad_signature_400(client, db):
    headers = await login(db, await create_member(db))
    sso, _ = _inbound()
    r = await client.get(
        "/auth/discourse-sso", params={"sso": sso, "sig": "deadbeef"}, headers=headers
    )
    assert r.status_code == 400


async def test_valid_round_trip(client, db):
    member = await create_member(db, email="forumuser@example.com", is_admin=True)
    mid = str(member.id)
    headers = await login(db, member)

    sso, sig = _inbound(nonce="abc123")
    r = await client.get(
        "/auth/discourse-sso", params={"sso": sso, "sig": sig}, headers=headers
    )
    assert r.status_code == 302

    location = r.headers["location"]
    assert location.startswith("https://forum.example/session/sso_login?")
    query, payload = _decode_redirect(location)

    # The response signature must verify over the base64 sso string.
    secret = get_settings().discourse_sso_secret
    expected_sig = hmac.new(secret.encode(), query["sso"].encode(), hashlib.sha256).hexdigest()
    assert query["sig"] == expected_sig

    # The response payload binds the forum identity to the Potluck member.
    assert payload["nonce"] == "abc123"
    assert payload["email"] == "forumuser@example.com"
    assert payload["external_id"] == mid
    assert payload["admin"] == "true"
    assert payload["username"] == "forumuser"


async def test_username_is_sanitized(client, db):
    member = await create_member(db, email="weird.name+tag@example.com")
    headers = await login(db, member)
    sso, sig = _inbound()
    r = await client.get(
        "/auth/discourse-sso", params={"sso": sso, "sig": sig}, headers=headers
    )
    _, payload = _decode_redirect(r.headers["location"])
    # '+' is not a legal Discourse username char and is stripped.
    assert payload["username"] == "weird.nametag"


async def test_falls_back_to_configured_discourse_url(client, db):
    """With no return_sso_url in the payload, redirect to DISCOURSE_URL."""
    member = await create_member(db)
    headers = await login(db, member)
    secret = get_settings().discourse_sso_secret
    sso = base64.b64encode(urllib.parse.urlencode({"nonce": "n1"}).encode()).decode()
    sig = hmac.new(secret.encode(), sso.encode(), hashlib.sha256).hexdigest()

    r = await client.get(
        "/auth/discourse-sso", params={"sso": sso, "sig": sig}, headers=headers
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith(
        f"{get_settings().discourse_url.rstrip('/')}/session/sso_login?"
    )
