"""Install a second, GPU-capable llama.cpp backend set beside the CPU one.

Why this exists
---------------
`pip install -r requirements.txt` gives Windows the default llama-cpp-python
wheel, which is CPU only: its `llama_cpp/lib/` holds `ggml-cpu.dll` and nothing
else, and `llama_supports_gpu_offload()` returns false. That is what shipped in
0.1.3, and it is why a GTX 1080 or an RX 6600 XT sat idle while the CPU ground
through every token.

abetlen publishes per-backend wheels of the *same* llama-cpp-python version
built from the *same* vendored llama.cpp commit. This script downloads one,
verifies it byte for byte, and unpacks only its `llama_cpp/lib/` into a sibling
directory:

    llama_cpp/lib          CPU, from requirements.txt. Untouched.
    llama_cpp/lib-vulkan   Vulkan, from here.

`potluck_local.services.llama_backend` picks between them at runtime. Because
both come from one wrapper version, a CPU/GPU comparison measures devices
rather than versions.

Usage
-----
    python scripts/vendor_gpu_backend.py              # every backend for this platform
    python scripts/vendor_gpu_backend.py --check      # verify, download nothing
    python scripts/vendor_gpu_backend.py --backend vulkan

`build.py` runs this before PyInstaller on Windows, so a normal
`make installer-bundle` picks it up with no extra step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# The llama-cpp-python version in requirements.txt. A backend set only belongs
# beside the CPU set if it is the same wrapper version, because the Python
# wrapper's ctypes declarations have to match the native library's ABI.
WRAPPER_VERSION = "0.3.35"

# llama.cpp commit vendored by that release, recorded so a future bump can
# confirm both halves moved together. Tag b10454.
LLAMA_CPP_COMMIT = "4df29be4f4c3673f428170fda944a5b19f743bb8"

_BASE = "https://github.com/abetlen/llama-cpp-python/releases/download"
_WHEEL = f"llama_cpp_python-{WRAPPER_VERSION}-py3-none-win_amd64.whl"

# The CPU wheel requirements.txt installs on Windows, recorded here so both
# halves of the pair are visible in one place. PyPI has no win_amd64 wheel for
# this version, so requirements.txt names this URL directly.
CPU_WHEEL_URL = f"{_BASE}/v{WRAPPER_VERSION}/{_WHEEL}"
CPU_WHEEL_SHA256 = "31590ea000d5aff6f05f1e428048e72318a83709288159a5bd4dabec530080bb"

# Pinned by URL, size and SHA-256. Nothing here resolves "latest".
#
# Vulkan is the GPU backend we ship on Windows: one build covers NVIDIA, AMD
# and Intel, it needs no vendor runtime beyond the installed graphics driver,
# and it costs 38 MB. The CUDA wheel (v0.3.35-cu124, 482,736,710 bytes) would
# add 476 MB to a 458 MB installer and could not be verified on any machine we
# have, so it is deliberately not shipped. If NVIDIA hardware becomes
# available, cu124 is the right variant: CUDA 12.x still targets Pascal
# (sm_61), CUDA 13 dropped it.
ARTIFACTS: dict[str, dict[str, dict]] = {
    "win32": {
        "vulkan": {
            "url": f"{_BASE}/v{WRAPPER_VERSION}-vulkan/{_WHEEL}",
            "sha256": "fa06c0c184801410be1ffdcc32cf5b57d92b23a6dff9e90e5870dea8710a233c",
            "size": 45480907,
            "target_dir": "lib-vulkan",
        }
    }
}

_MARKER = ".potluck-backend.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def llama_cpp_lib_root() -> Path:
    """`llama_cpp/` in the active environment, without importing the library."""
    import importlib.util

    spec = importlib.util.find_spec("llama_cpp")
    if spec is None or not spec.origin:
        raise SystemExit(
            "llama_cpp is not installed in this environment; "
            "run `pip install -r requirements.txt` first"
        )
    return Path(spec.origin).parent


def assert_wrapper_version() -> None:
    """Refuse to mix a vendored native library with a different wrapper version.

    The wrapper's ctypes struct definitions track the llama.cpp commit it was
    built against. Pairing them across versions gives silent memory corruption
    rather than an honest import error, so check before writing anything.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("llama-cpp-python")
    except PackageNotFoundError:
        raise SystemExit(
            "llama-cpp-python is not installed; run `pip install -r requirements.txt` first"
        ) from None
    if installed != WRAPPER_VERSION:
        raise SystemExit(
            f"llama-cpp-python {installed} is installed but the vendored backend sets are "
            f"built for {WRAPPER_VERSION}.\n"
            "Pin requirements.txt and this script to the same version before building."
        )


def _installed_marker(target: Path) -> dict | None:
    try:
        return json.loads((target / _MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _download(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as response, open(dest, "wb") as fh:
        shutil.copyfileobj(response, fh)


def install_backend(name: str, spec: dict, *, check_only: bool = False) -> bool:
    """Install one backend set. Returns True when it is present and verified."""
    pkg = llama_cpp_lib_root()
    target = pkg / spec["target_dir"]

    marker = _installed_marker(target)
    if marker and marker.get("sha256") == spec["sha256"]:
        dll_count = len(list(target.glob("*.dll")))
        print(f"  {name}: already installed at {target} ({dll_count} libraries)")
        return True

    if check_only:
        print(f"  {name}: MISSING at {target}")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        wheel = Path(tmp) / _WHEEL
        _download(spec["url"], wheel)

        actual_size = wheel.stat().st_size
        if actual_size != spec["size"]:
            raise SystemExit(
                f"{name}: size mismatch, expected {spec['size']} bytes, got {actual_size}"
            )
        actual_sha = _sha256(wheel)
        if actual_sha != spec["sha256"]:
            raise SystemExit(
                f"{name}: sha256 mismatch\n  expected {spec['sha256']}\n  got      {actual_sha}"
            )
        print(f"  {name}: verified {actual_size} bytes, sha256 {actual_sha}")

        staged = Path(tmp) / "unpacked"
        with zipfile.ZipFile(wheel) as zf:
            members = [
                m
                for m in zf.namelist()
                if m.startswith("llama_cpp/lib/") and not m.endswith("/")
            ]
            if not members:
                raise SystemExit(f"{name}: wheel has no llama_cpp/lib/ payload")
            zf.extractall(staged, members=members)

        source = staged / "llama_cpp" / "lib"
        # Import libraries are link-time only; they are dead weight in a bundle.
        payload = [f for f in sorted(source.iterdir()) if f.suffix.lower() != ".lib"]
        if not payload:
            raise SystemExit(f"{name}: nothing to install after filtering")

        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for f in payload:
            shutil.copy2(f, target / f.name)

        (target / _MARKER).write_text(
            json.dumps(
                {
                    "backend": name,
                    "wrapper_version": WRAPPER_VERSION,
                    "llama_cpp_commit": LLAMA_CPP_COMMIT,
                    "url": spec["url"],
                    "sha256": spec["sha256"],
                    "size": spec["size"],
                    "files": [f.name for f in payload],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        total_mb = sum(f.stat().st_size for f in payload) / (1024 * 1024)
        print(f"  {name}: installed {len(payload)} libraries ({total_mb:.1f} MB) to {target}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", help="install only this backend (default: all for this platform)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is installed and exit non-zero if anything is missing",
    )
    args = parser.parse_args()

    platform_artifacts = ARTIFACTS.get(sys.platform, {})
    if not platform_artifacts:
        print(f"No extra llama.cpp backend sets are defined for {sys.platform}; nothing to do.")
        return 0

    wanted = platform_artifacts
    if args.backend:
        if args.backend not in platform_artifacts:
            print(
                f"error: no {args.backend!r} backend for {sys.platform}; "
                f"known: {sorted(platform_artifacts)}",
                file=sys.stderr,
            )
            return 2
        wanted = {args.backend: platform_artifacts[args.backend]}

    assert_wrapper_version()
    print(f"llama.cpp backend sets for {sys.platform} (wrapper {WRAPPER_VERSION}):")
    ok = True
    for name, spec in wanted.items():
        ok = install_backend(name, spec, check_only=args.check) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
