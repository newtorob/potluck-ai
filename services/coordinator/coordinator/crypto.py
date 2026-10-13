"""Shared Ed25519 public-key decode/verify helpers.

Extracted from `services/attestation.py` so the same validation logic is
used everywhere the coordinator accepts a caller-supplied Ed25519 public
key: the WS attestation handshake (`services/attestation.py`), the
session-authenticated device-key registration endpoint
(`routers/circles.py`), and the optional DERP-token proof-of-possession
check (`routers/mesh.py`). One implementation means one place to harden.
"""
from __future__ import annotations

import base64

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

__all__ = [
    "KeyDecodeError",
    "b64url_decode",
    "decode_ed25519_public_key",
    "verify_ed25519_signature",
]


class KeyDecodeError(Exception):
    """Raised when a caller-supplied key or signature string fails to
    decode or validate. The message is safe to relay to the caller."""


def b64url_decode(s: str) -> bytes:
    """Decode base64url with optional padding stripped."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def decode_ed25519_public_key(b64: str, *, field_name: str = "public_key") -> Ed25519PublicKey:
    """Decode + validate a base64url-encoded Ed25519 public key (32 raw bytes).

    Raises KeyDecodeError with a caller-safe message on any failure.
    """
    try:
        raw = b64url_decode(b64)
    except Exception as e:
        raise KeyDecodeError(f"{field_name}: not valid base64url ({e})") from e
    if len(raw) != 32:
        raise KeyDecodeError(f"{field_name}: expected 32 raw bytes, got {len(raw)}")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as e:
        raise KeyDecodeError(f"{field_name}: not a valid Ed25519 key ({e})") from e


def verify_ed25519_signature(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    """Verify `signature_b64` (base64url) over `message` using the base64url
    Ed25519 public key `public_key_b64`. Returns False on any decode error
    or signature mismatch - never raises."""
    try:
        pub = decode_ed25519_public_key(public_key_b64)
        sig = b64url_decode(signature_b64)
    except KeyDecodeError:
        return False
    except Exception:
        return False
    try:
        pub.verify(sig, message)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False
