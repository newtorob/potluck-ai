"""Exact GGUF identity shared by project locks, serving and model management."""
from pathlib import Path
from potluck_local.utils.crypto import sha256_file


class ModelIdentityError(RuntimeError):
    category = "model_mismatch"


def file_stamp(path: Path) -> tuple:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def artifact_identity(path: Path) -> tuple[str, tuple]:
    before = file_stamp(path)
    digest = sha256_file(path)
    if file_stamp(path) != before:
        raise OSError("Model file changed during verification; retry once the download completes.")
    return digest, before


def lock_headers(sha256: str | None) -> dict[str, str]:
    # Acknowledges enforcement, not successful generation. Errors can still follow in SSE.
    return {"X-Potluck-Model-Lock": "1", "X-Potluck-Model-SHA256": sha256} if sha256 else {}


# Lockfiles can come from a cloned repository. Limit automatic downloads to the
# same public Hub used by the catalog; never accept credentials or local URLs.
def valid_source_url(url: str) -> bool:
    import re
    return bool(re.fullmatch(
        r"https://huggingface\.co/[A-Za-z0-9_-]+/[A-Za-z0-9._-]+/resolve/[A-Za-z0-9_-]+/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.gguf", url
    ))
