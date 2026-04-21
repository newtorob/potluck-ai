"""Cryptographic utilities.

sha256_file is used by ModelDownloader to verify downloaded weights before
promoting the .partial file to .gguf.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 8 * 1024  # 8 KB — keeps memory flat for multi-GB model files


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of a file.

    Reads in 8 KB chunks so the full file is never held in memory.
    Raises FileNotFoundError if the path does not exist.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()
