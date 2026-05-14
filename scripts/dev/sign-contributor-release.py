#!/usr/bin/env python3
"""Potluck contributor release signing helper.

The Potluck coordinator only accepts WebSocket connections from contributor
clients whose `binary_hash` is in the `contributor_releases` table, signed
by the co-op release key. This script handles the signing workflow:

    keygen     — generate a new Ed25519 release keypair (one-time bootstrap)
    info       — show fingerprint of the current release public key
    sign       — sign a built artifact (wheel, tarball, binary), emit hash + sig
    verify     — verify a signature against an artifact
    emit-sql   — sign + emit a contributor_releases INSERT statement

The private key lives ONLY on the signer's machine. It NEVER goes to
stdout, NEVER appears in logs, and is created with mode 0o600.

See docs/security/release-key.md for the full operational runbook,
threat model, rotation procedure, and migration path to HSM-backed
signing once the co-op has governance around it.

Dependency: `cryptography` package. Either run inside the coordinator's
venv (services/coordinator/.venv) or `pip install cryptography>=44`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature
except ImportError:
    sys.stderr.write(
        "error: missing dependency `cryptography`. Install with:\n"
        "  pip install 'cryptography>=44'\n"
        "Or run from the coordinator venv: services/coordinator/.venv/bin/python\n"
    )
    sys.exit(1)


# Default key location. Single-machine for now (single signer in the
# bootstrap phase). When the co-op has governance for key custody, this
# becomes an HSM-backed key — see docs/security/release-key.md.
DEFAULT_KEY_DIR = Path.home() / ".potluck" / "release-keys" / "dev"
PRIV_KEY_FILENAME = "private.pem"
PUB_KEY_FILENAME = "public.pem"


def _priv_path(key_dir: Path) -> Path:
    return key_dir / PRIV_KEY_FILENAME


def _pub_path(key_dir: Path) -> Path:
    return key_dir / PUB_KEY_FILENAME


def _load_private_key(key_dir: Path) -> Ed25519PrivateKey:
    path = _priv_path(key_dir)
    if not path.exists():
        sys.stderr.write(
            f"error: no private key at {path}\n"
            f"  run: {sys.argv[0]} keygen --key-dir {key_dir}\n"
        )
        sys.exit(2)
    with path.open("rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.stderr.write(f"error: {path} is not an Ed25519 private key\n")
        sys.exit(2)
    return key


def _load_public_key(key_dir: Path) -> Ed25519PublicKey:
    path = _pub_path(key_dir)
    if not path.exists():
        sys.stderr.write(f"error: no public key at {path}\n")
        sys.exit(2)
    with path.open("rb") as f:
        key = serialization.load_pem_public_key(f.read())
    if not isinstance(key, Ed25519PublicKey):
        sys.stderr.write(f"error: {path} is not an Ed25519 public key\n")
        sys.exit(2)
    return key


def _public_fingerprint(pub: Ed25519PublicKey) -> str:
    """Short, human-comparable fingerprint: first 16 hex chars of SHA256(raw pubkey)."""
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _hash_file(path: Path) -> str:
    """SHA256(file contents) as lowercase hex."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- subcommands ----------


def cmd_keygen(args: argparse.Namespace) -> None:
    key_dir = Path(args.key_dir)
    priv_path = _priv_path(key_dir)
    pub_path = _pub_path(key_dir)

    if priv_path.exists() and not args.force:
        sys.stderr.write(
            f"error: private key already exists at {priv_path}\n"
            f"  use --force to overwrite (NOT RECOMMENDED — see docs/security/release-key.md\n"
            f"  for the rotation procedure)\n"
        )
        sys.exit(3)

    key_dir.mkdir(parents=True, exist_ok=True)
    # Tighten directory perms — best effort on POSIX, no-op on Windows.
    try:
        os.chmod(key_dir, 0o700)
    except OSError:
        pass

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Write private key with restricted perms BEFORE writing contents.
    priv_path.touch(mode=0o600, exist_ok=False)
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)

    print(f"generated Ed25519 release keypair")
    print(f"  private key: {priv_path}  (mode 0600 — DO NOT commit, paste, or share)")
    print(f"  public key:  {pub_path}")
    print(f"  fingerprint: {_public_fingerprint(pub)}")
    print()
    print("next steps:")
    print(f"  1. back up the private key — see docs/security/release-key.md")
    print(f"  2. seed contributor_releases with a dev binary hash:")
    print(f"     {sys.argv[0]} emit-sql <built-artifact> --version 0.0.1-dev")


def cmd_info(args: argparse.Namespace) -> None:
    key_dir = Path(args.key_dir)
    pub = _load_public_key(key_dir)
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    print(f"key dir:     {key_dir}")
    print(f"fingerprint: {_public_fingerprint(pub)}")
    print(f"private key: {'present' if _priv_path(key_dir).exists() else 'MISSING'}")
    print()
    print(pub_pem.strip())


def cmd_sign(args: argparse.Namespace) -> None:
    key_dir = Path(args.key_dir)
    artifact = Path(args.artifact)
    if not artifact.exists():
        sys.stderr.write(f"error: artifact not found: {artifact}\n")
        sys.exit(2)

    priv = _load_private_key(key_dir)
    pub = priv.public_key()

    artifact_hash = _hash_file(artifact)
    # Sign the hex-encoded hash so signature verification is deterministic
    # whether the verifier rehashes the file or just verifies the hash.
    sig = priv.sign(artifact_hash.encode("ascii"))
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")

    print(f"artifact:    {artifact}")
    print(f"size:        {artifact.stat().st_size} bytes")
    print(f"sha256:      {artifact_hash}")
    print(f"signer:      {_public_fingerprint(pub)}")
    print(f"signature:   {sig_b64}")


def cmd_verify(args: argparse.Namespace) -> None:
    key_dir = Path(args.key_dir)
    artifact = Path(args.artifact)
    if not artifact.exists():
        sys.stderr.write(f"error: artifact not found: {artifact}\n")
        sys.exit(2)

    pub = _load_public_key(key_dir)
    artifact_hash = _hash_file(artifact)

    sig_b64 = args.signature
    # Restore base64 padding stripped by urlsafe_b64encode().rstrip("=").
    pad = "=" * (-len(sig_b64) % 4)
    try:
        sig = base64.urlsafe_b64decode(sig_b64 + pad)
    except Exception as e:
        sys.stderr.write(f"error: signature is not valid base64: {e}\n")
        sys.exit(2)

    try:
        pub.verify(sig, artifact_hash.encode("ascii"))
    except InvalidSignature:
        print(f"INVALID — signature does not match artifact + public key")
        sys.exit(1)

    print(f"VALID — signature matches {artifact} under fingerprint {_public_fingerprint(pub)}")


def cmd_emit_sql(args: argparse.Namespace) -> None:
    key_dir = Path(args.key_dir)
    artifact = Path(args.artifact)
    if not artifact.exists():
        sys.stderr.write(f"error: artifact not found: {artifact}\n")
        sys.exit(2)

    priv = _load_private_key(key_dir)
    pub = priv.public_key()

    artifact_hash = _hash_file(artifact)
    sig = priv.sign(artifact_hash.encode("ascii"))
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    fingerprint = _public_fingerprint(pub)

    notes_sql = "NULL" if not args.notes else f"'{args.notes.replace(chr(39), chr(39) + chr(39))}'"

    sql = (
        "-- Generated by scripts/dev/sign-contributor-release.py\n"
        f"-- Artifact: {artifact}  ({artifact.stat().st_size} bytes)\n"
        f"-- Signed by release-key fingerprint: {fingerprint}\n"
        f"-- Signature (base64url): {sig_b64}\n"
        "INSERT INTO contributor_releases "
        "(binary_hash, version, signed_at, signer_key, notes) VALUES\n"
        f"  ('{artifact_hash}', '{args.version}', now(), '{fingerprint}', {notes_sql});"
    )
    print(sql)


# ---------- argparse ----------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sign-contributor-release",
        description=(
            "Manage the Potluck Ed25519 contributor release-signing key and "
            "produce signatures for built artifacts."
        ),
    )
    parser.add_argument(
        "--key-dir",
        default=str(DEFAULT_KEY_DIR),
        help=(
            f"directory holding {PRIV_KEY_FILENAME} and {PUB_KEY_FILENAME} "
            f"(default: {DEFAULT_KEY_DIR})"
        ),
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_keygen = sub.add_parser("keygen", help="generate a new release keypair")
    p_keygen.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing private key (DANGEROUS — see runbook)",
    )
    p_keygen.set_defaults(func=cmd_keygen)

    p_info = sub.add_parser("info", help="show fingerprint of the current public key")
    p_info.set_defaults(func=cmd_info)

    p_sign = sub.add_parser("sign", help="sign an artifact, print hash + signature")
    p_sign.add_argument("artifact", help="path to the file to sign")
    p_sign.set_defaults(func=cmd_sign)

    p_verify = sub.add_parser("verify", help="verify a signature against an artifact")
    p_verify.add_argument("artifact", help="path to the file")
    p_verify.add_argument("signature", help="base64url signature to verify")
    p_verify.set_defaults(func=cmd_verify)

    p_emit = sub.add_parser(
        "emit-sql",
        help="sign and print a contributor_releases INSERT statement",
    )
    p_emit.add_argument("artifact", help="path to the file to sign")
    p_emit.add_argument("--version", required=True, help="release version label, e.g. 0.0.1-dev")
    p_emit.add_argument("--notes", default="", help="optional notes column value")
    p_emit.set_defaults(func=cmd_emit_sql)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
