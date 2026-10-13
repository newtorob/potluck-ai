"""Per-peer Ed25519 keypair management.

This is the peer's stable identity to the coordinator - distinct from
the release-signing key (which lives only on the signer's machine, not
on member machines). The same per-peer key reconnects to the same
`peers` row across restarts.

Generated lazily on first launch, persisted to platform-appropriate
user-data dir, mode 0600 on POSIX.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger(__name__)

PRIV_KEY_FILENAME = "peer-key.pem"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def public_key_b64(pub: Ed25519PublicKey) -> str:
    """The 32 raw public-key bytes, base64url-encoded, no padding."""
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url(raw)


def ensure_keypair(key_dir: Path) -> Ed25519PrivateKey:
    """Load the peer's keypair, generating + persisting one if missing."""
    key_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_dir, 0o700)
    except OSError:
        # Windows or unusual filesystem - best effort.
        pass

    priv_path = key_dir / PRIV_KEY_FILENAME
    if priv_path.exists():
        with priv_path.open("rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError(f"{priv_path} is not an Ed25519 private key")
        logger.info("peer_key_loaded", extra={"path": str(priv_path)})
        return key

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    priv_path.touch(mode=0o600, exist_ok=False)
    priv_path.write_bytes(pem)
    logger.info("peer_key_generated", extra={"path": str(priv_path)})
    return key
