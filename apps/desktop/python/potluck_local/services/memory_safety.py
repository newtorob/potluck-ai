"""Narrow credential redaction for persistent memory, not general PII filtering.

Known token formats and explicitly labelled credential values are removed.
Ordinary identifiers, hashes, paths and references to authentication stay intact.
No matched value is logged, retained in an exception, or sent to another service.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

REDACTED = "[REDACTED CREDENTIAL]"

_TOKEN = re.compile(
    r"(?<![\w-])(?:"
    r"sk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|rnd_[A-Za-z0-9]{20,}"
    r"|sb_secret_[A-Za-z0-9_-]{20,}"
    r"|shp(?:ss|at|ca|pa)_[A-Fa-f0-9]{32,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")(?![\w-])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"[\s\S]*?(?:-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\Z)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9_./~+\-=]{16,})")
_LABELLED = re.compile(
    r"(?i)(?P<label>\b(?:[a-z0-9]+[_-])*(?:api[ _-]?key|api[ _-]?token|"
    r"access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|secret[ _-]?key|"
    r"password|passwd|secret|token)\b[\"'`]*\s*(?:[:=]|\bis\b)\s*[\"'`]*)"
    r"(?P<value>[^\s\"'`<>;,\)\]}]{8,})"
)
_PLACEHOLDERS = frozenset({
    "redacted", "changeme", "your-api-key", "your_api_key", "your-token",
    "your_token", "your-password", "your_password", "placeholder", "required",
    "optional", "undefined", "example", "not-configured",
})


def _placeholder(value: str) -> bool:
    return value.lower() in _PLACEHOLDERS or value.startswith(("$", "[REDACTED"))


def redact_credentials(text: str) -> str:
    """Keep non-secret context and replace credential-shaped spans. Idempotent."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _TOKEN.sub(REDACTED, text)
    text = _BEARER.sub(
        lambda m: m[1] + (m[2] if _placeholder(m[2]) else REDACTED), text,
    )

    def labelled(match: re.Match) -> str:
        value = match["value"]
        # Shopify's API key is its public OAuth client id, including the Vite
        # build variable. Provider-shaped secrets are already redacted above.
        if re.search(r"(?i)\b(?:[a-z0-9]+_)*shopify_api_key\b", match["label"]):
            return match[0]
        # Generic labels in prose need a value that looks like a credential.
        # Passwords may be all letters, while token/secret examples should not
        # eat phrases such as "token: authentication".
        password = re.search(r"(?i)pass(?:word|wd)", match["label"]) is not None
        mixed = any(c.isalpha() for c in value) and any(c.isdigit() for c in value)
        if _placeholder(value) or not (password or mixed):
            return match[0]
        return match["label"] + REDACTED

    return _LABELLED.sub(labelled, text)


async def redact_stored_memories(db: aiosqlite.Connection) -> int:
    """Repair legacy memory content in the caller's transaction. Counts only.

    Update timestamps and discard stale vectors so both incremental export and
    embedding backfill see the change. Preserve ids, pins and non-secret text.
    Scanning at startup also covers imports written by an older running client.
    """
    from potluck_local.utils.time import now_iso

    cursor = await db.execute("SELECT id, content FROM memories")
    changed = 0
    for row in await cursor.fetchall():
        memory_id, content = row[0], row[1]
        safe = redact_credentials(content)
        if safe == content:
            continue
        await db.execute(
            "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
            (safe, now_iso(), memory_id),
        )
        await db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (memory_id,))
        changed += 1
    return changed
