"""Tests for the DERP-token proof-of-possession (PoP) hardening.

Background (HIGH-severity audit finding): POST /mesh/derp-token previously
accepted possession of a registered (non-secret) `wg_public_key` as the ONLY
credential — anyone who observed a node's WG pubkey could mint a DERP-relay
token AS that node and hijack its relay session.

Fix (back-compat, additive): `DERPTokenRequest` gained OPTIONAL `nonce` +
`signature` fields. When both are present, the coordinator verifies the
signature against a separately-registered `MeshNode.signing_public_key`
(Ed25519 — distinct from the WG/Curve25519 key). When absent, legacy
behaviour is preserved (a warning is logged) unless `derp_pop_required=True`.

Coverage:
  - legacy path unaffected when signature/nonce are both absent.
  - `derp_pop_required=True` rejects a request with no signature.
  - nonce+signature must both be present or both absent (400 on mismatch).
  - stale/future nonce rejected (replay-window bound).
  - signature required but node has no signing_public_key registered -> 401.
  - a genuinely valid signature is accepted.
  - a bad/forged signature is rejected.
  - POST /mesh/enroll persists an optional signing_public_key, and rejects a
    malformed one with 400 without touching existing enrollment behaviour.
"""
from __future__ import annotations

import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import text

from coordinator.models.household import Household
from coordinator.models.mesh_node import MeshNode
from coordinator.schemas.mesh import DERPTokenRequest, EnrollRequest
from coordinator.routers.mesh import _verify_derp_pop, enroll, issue_derp_token


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _fresh_wg_pubkey() -> str:
    # WG pubkeys aren't Ed25519-validated by the schema/router — any
    # sufficiently long opaque string is accepted, matching production
    # (wg genkey output is base64-standard with padding).
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _random_cidr() -> str:
    # Unique /24 inside the CGNAT range households actually use, chosen
    # randomly to avoid colliding with the real allocator's sequential
    # assignment or with other parallel test runs.
    b = 64 + secrets.randbelow(64)
    c = secrets.randbelow(256)
    return f"100.{b}.{c}.0/24"


async def _seed_household_and_node(
    db, *, signing_public_key: str | None = None
) -> tuple[uuid.UUID, str]:
    household = Household(
        owner_member_id=uuid.uuid4(), name="pop-test", mesh_cidr=_random_cidr()
    )
    db.add(household)
    await db.flush()

    wg_pubkey = _fresh_wg_pubkey()
    node = MeshNode(
        household_id=household.id,
        node_name=f"node-{uuid.uuid4().hex[:8]}",
        mesh_ip="100.99.0.2",
        wg_public_key=wg_pubkey,
        signing_public_key=signing_public_key,
    )
    db.add(node)
    await db.flush()
    return household.id, wg_pubkey


@pytest.fixture(autouse=True)
def _derp_jwt_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POTLUCK_DERP_JWT_KEY", "test-derp-jwt-key")


@pytest.fixture(autouse=True)
def _reset_derp_pop_flag(monkeypatch: pytest.MonkeyPatch):
    from coordinator.config import get_settings

    monkeypatch.delenv("DERP_POP_REQUIRED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ─── legacy path (no signature) — unaffected by default ─────────────────────


@pytest.mark.asyncio
async def test_legacy_request_without_signature_still_works(db_session) -> None:
    _, wg_pubkey = await _seed_household_and_node(db_session)
    resp = await issue_derp_token(
        DERPTokenRequest(wg_public_key=wg_pubkey), auth_node=None, db=db_session
    )
    assert resp.token
    assert resp.region == "ord"


@pytest.mark.asyncio
async def test_unknown_wg_pubkey_still_404s(db_session) -> None:
    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=_fresh_wg_pubkey()), auth_node=None, db=db_session
        )
    assert exc.value.status_code == 404


# ─── nonce/signature pairing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nonce_without_signature_400(db_session) -> None:
    _, wg_pubkey = await _seed_household_and_node(db_session)
    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=wg_pubkey, nonce="12345"), auth_node=None, db=db_session
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_signature_without_nonce_400(db_session) -> None:
    _, wg_pubkey = await _seed_household_and_node(db_session)
    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=wg_pubkey, signature="bogus"), auth_node=None, db=db_session
        )
    assert exc.value.status_code == 400


# ─── valid PoP ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_signature_accepted(db_session) -> None:
    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    region = "ord"
    nonce = str(int(datetime.now(timezone.utc).timestamp()))
    message = f"derp-token:{wg_pubkey}:{region}:{nonce}".encode()
    sig = _b64url(priv.sign(message))

    resp = await issue_derp_token(
        DERPTokenRequest(wg_public_key=wg_pubkey, region=region, nonce=nonce, signature=sig),
        auth_node=None, db=db_session,
    )
    assert resp.token
    assert resp.region == region


@pytest.mark.asyncio
async def test_forged_signature_rejected(db_session) -> None:
    """Wrong private key signs the nonce -> 401, not accepted."""
    real_priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(real_priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    attacker_priv = Ed25519PrivateKey.generate()
    region = "ord"
    nonce = str(int(datetime.now(timezone.utc).timestamp()))
    message = f"derp-token:{wg_pubkey}:{region}:{nonce}".encode()
    forged_sig = _b64url(attacker_priv.sign(message))

    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(
                wg_public_key=wg_pubkey, region=region, nonce=nonce, signature=forged_sig
            ),
            auth_node=None, db=db_session,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_signature_over_wrong_message_rejected(db_session) -> None:
    """A signature valid for a DIFFERENT wg_public_key/region/nonce triple
    must not verify — proves the message is actually bound, not just any
    signature from the right key."""
    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    nonce = str(int(datetime.now(timezone.utc).timestamp()))
    wrong_message = f"derp-token:{wg_pubkey}:us-east:{nonce}".encode()  # wrong region
    sig = _b64url(priv.sign(wrong_message))

    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=wg_pubkey, region="ord", nonce=nonce, signature=sig),
            auth_node=None, db=db_session,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_signature_without_registered_signing_key_401(db_session) -> None:
    """Caller presents a signature but the node never registered a
    signing_public_key (e.g. enrolled before PoP hardening) -> 401, not a
    silent fallback to the legacy check."""
    priv = Ed25519PrivateKey.generate()
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=None)

    nonce = str(int(datetime.now(timezone.utc).timestamp()))
    message = f"derp-token:{wg_pubkey}:ord:{nonce}".encode()
    sig = _b64url(priv.sign(message))

    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=wg_pubkey, nonce=nonce, signature=sig),
            auth_node=None, db=db_session,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_stale_nonce_rejected(db_session) -> None:
    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    nonce = str(int(stale_time.timestamp()))
    message = f"derp-token:{wg_pubkey}:ord:{nonce}".encode()
    sig = _b64url(priv.sign(message))

    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(wg_public_key=wg_pubkey, nonce=nonce, signature=sig),
            auth_node=None, db=db_session,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_non_numeric_nonce_400(db_session) -> None:
    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(
            DERPTokenRequest(
                wg_public_key=wg_pubkey, nonce="not-a-timestamp", signature="bogus"
            ),
            auth_node=None, db=db_session,
        )
    assert exc.value.status_code == 400


# ─── derp_pop_required flag ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pop_required_rejects_missing_signature(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator.config import get_settings

    monkeypatch.setenv("DERP_POP_REQUIRED", "true")
    get_settings.cache_clear()

    _, wg_pubkey = await _seed_household_and_node(db_session)
    with pytest.raises(HTTPException) as exc:
        await issue_derp_token(DERPTokenRequest(wg_public_key=wg_pubkey), auth_node=None, db=db_session)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_pop_required_accepts_valid_signature(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator.config import get_settings

    monkeypatch.setenv("DERP_POP_REQUIRED", "true")
    get_settings.cache_clear()

    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    _, wg_pubkey = await _seed_household_and_node(db_session, signing_public_key=signing_pub)

    nonce = str(int(datetime.now(timezone.utc).timestamp()))
    message = f"derp-token:{wg_pubkey}:ord:{nonce}".encode()
    sig = _b64url(priv.sign(message))

    resp = await issue_derp_token(
        DERPTokenRequest(wg_public_key=wg_pubkey, nonce=nonce, signature=sig),
        auth_node=None, db=db_session,
    )
    assert resp.token


# ─── enrollment: optional signing_public_key ─────────────────────────────────


@pytest.mark.asyncio
async def test_enroll_persists_optional_signing_key(session_factory) -> None:
    from coordinator.models.household import Household
    from coordinator.models.pairing import PairingToken

    household_id: uuid.UUID
    token_value = f"tok-{uuid.uuid4().hex[:20]}"
    async with session_factory.factory() as db:
        async with db.begin():
            household = Household(
                owner_member_id=uuid.uuid4(), name="enroll-pop-test", mesh_cidr=_random_cidr()
            )
            db.add(household)
            await db.flush()
            household_id = household.id
            db.add(
                PairingToken(
                    token=token_value,
                    household_id=household_id,
                    issued_by_member_id=household.owner_member_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )

    priv = Ed25519PrivateKey.generate()
    signing_pub = _b64url(priv.public_key().public_bytes_raw())
    wg_pubkey = _fresh_wg_pubkey()

    node_id: uuid.UUID | None = None
    try:
        async with session_factory.factory() as db:
            resp = await enroll(
                EnrollRequest(
                    pairing_token=token_value,
                    wg_public_key=wg_pubkey,
                    node_name="pop-node",
                    signing_public_key=signing_pub,
                ),
                db=db,
            )
        node_id = resp.node_id

        async with session_factory.factory() as verify_db:
            row = await verify_db.get(MeshNode, node_id)
            assert row is not None
            assert row.signing_public_key == signing_pub
    finally:
        async with session_factory.factory() as cleanup_db:
            async with cleanup_db.begin():
                if node_id is not None:
                    await cleanup_db.execute(
                        text("DELETE FROM mesh_nodes WHERE id = :i"), {"i": node_id}
                    )
                await cleanup_db.execute(
                    text("DELETE FROM pairing_tokens WHERE token = :t"), {"t": token_value}
                )
                await cleanup_db.execute(
                    text("DELETE FROM households WHERE id = :h"), {"h": household_id}
                )


@pytest.mark.asyncio
async def test_enroll_rejects_malformed_signing_key(session_factory) -> None:
    from coordinator.models.household import Household
    from coordinator.models.pairing import PairingToken

    household_id: uuid.UUID
    token_value = f"tok-{uuid.uuid4().hex[:20]}"
    async with session_factory.factory() as db:
        async with db.begin():
            household = Household(
                owner_member_id=uuid.uuid4(), name="enroll-badkey-test", mesh_cidr=_random_cidr()
            )
            db.add(household)
            await db.flush()
            household_id = household.id
            db.add(
                PairingToken(
                    token=token_value,
                    household_id=household_id,
                    issued_by_member_id=household.owner_member_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )

    try:
        async with session_factory.factory() as db:
            with pytest.raises(HTTPException) as exc:
                await enroll(
                    EnrollRequest(
                        pairing_token=token_value,
                        wg_public_key=_fresh_wg_pubkey(),
                        node_name="bad-pop-node",
                        # Valid base64url but decodes to 30 bytes, not the
                        # required 32 — trips the Ed25519 length check.
                        signing_public_key="A" * 40,
                    ),
                    db=db,
                )
            assert exc.value.status_code == 400
    finally:
        async with session_factory.factory() as cleanup_db:
            async with cleanup_db.begin():
                await cleanup_db.execute(
                    text("DELETE FROM pairing_tokens WHERE token = :t"), {"t": token_value}
                )
                await cleanup_db.execute(
                    text("DELETE FROM households WHERE id = :h"), {"h": household_id}
                )


# ─── direct helper coverage ───────────────────────────────────────────────────


def test_verify_derp_pop_rejects_no_signing_key_directly() -> None:
    node = MeshNode(
        id=uuid.uuid4(),
        household_id=uuid.uuid4(),
        node_name="n",
        mesh_ip="100.99.0.3",
        wg_public_key=_fresh_wg_pubkey(),
        signing_public_key=None,
    )
    req = DERPTokenRequest(
        wg_public_key=node.wg_public_key,
        nonce=str(int(datetime.now(timezone.utc).timestamp())),
        signature="bogus",
    )
    with pytest.raises(HTTPException) as exc:
        _verify_derp_pop(node, req)
    assert exc.value.status_code == 401
