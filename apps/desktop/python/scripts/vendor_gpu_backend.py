"""Install a second, GPU-capable llama.cpp backend set beside the CPU one.

Why this exists
---------------
Windows and Linux x86-64 use a pinned CPU-only wheel as the fallback. GPU
offload requires an additional backend compiled for the same wrapper ABI.

abetlen publishes per-backend wheels of the *same* llama-cpp-python version
built from the *same* vendored llama.cpp commit. This script downloads one,
verifies it byte for byte, and installs an isolated sibling backend set:

    llama_cpp/lib          CPU, from requirements.txt. Untouched.
    llama_cpp/lib-vulkan   Vulkan, with its relative wheel dependency layout
                           on Linux and flat DLLs on Windows.

`potluck_local.services.llama_backend` picks between them at runtime. Because
both come from one wrapper version, a CPU/GPU comparison measures devices
rather than versions.

Usage
-----
    python scripts/vendor_gpu_backend.py              # every backend for this platform
    python scripts/vendor_gpu_backend.py --check      # verify, download nothing
    python scripts/vendor_gpu_backend.py --backend vulkan

`make sidecar-setup` and `build.py` run this before PyInstaller. After
collection, build.py verifies every payload file again. Linux and Windows
pins are independent; each CPU/GPU pair must match within its platform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

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
            "required": ["llama.dll", "ggml.dll", "ggml-base.dll", "ggml-vulkan.dll"],
        }
    },
    "linux": {
        "vulkan": {
            # Match the wrapper already shipped in desktop 0.1.7 on Linux.
            "wrapper_version": "0.3.36",
            "llama_cpp_commit": "0c1e57098bba43ac29e6e3b677cdceebdd22334f",
            "url": f"{_BASE}/v0.3.36-vulkan/"
            "llama_cpp_python-0.3.36-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
            "sha256": "00eeeb1771c2ddad42c65b662372e5785caef930c7b03a054169207c474478f7",
            "size": 127484094,
            "target_dir": "lib-vulkan",
            # Keep auditwheel's relative RPATH intact, isolated from the CPU
            # wheel's libraries. Copying just llama_cpp/lib silently loses
            # the hashed dependencies in llama_cpp_python.libs.
            "layout": "wheel",
            "required": [
                "llama_cpp/lib/libllama.so", "llama_cpp/lib/libggml.so",
                "llama_cpp/lib/libggml-base.so", "llama_cpp/lib/libggml-vulkan.so",
            ],
        }
    },
}

_MARKER = ".potluck-backend.json"


def platform_artifacts() -> dict[str, dict]:
    # The public Linux installer is x86-64. Never put x86 libraries in an
    # ARM/RISC-V developer environment.
    if sys.platform == "linux" and platform.machine().lower() not in ("x86_64", "amd64"):
        return {}
    return ARTIFACTS.get(sys.platform, {})


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


def assert_wrapper_version(expected: str = WRAPPER_VERSION) -> None:
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
    if installed != expected:
        raise SystemExit(
            f"llama-cpp-python {installed} is installed but the vendored backend sets are "
            f"built for {expected}.\n"
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


def verify_backend(target: Path, spec: dict) -> bool:
    """Verify the payload too, not just a marker left by a previous build."""
    marker = _installed_marker(target)
    if not isinstance(marker, dict) or marker.get("sha256") != spec["sha256"]:
        return False
    files = marker.get("files")
    if marker.get("wrapper_version") != spec.get("wrapper_version", WRAPPER_VERSION) or not isinstance(files, dict):
        return False
    if marker.get("llama_cpp_commit") != spec.get("llama_cpp_commit", LLAMA_CPP_COMMIT):
        return False
    if not files or any(name not in files for name in spec["required"]):
        return False
    if spec.get("layout") == "wheel" and not any(
        name.startswith("llama_cpp_python.libs/") for name in files
    ):
        return False
    for name, digest in files.items():
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or "\\" in name:
            return False
        path = target / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(target.resolve()):
            return False
        if _sha256(path) != digest:
            return False
    return True


def install_backend(name: str, spec: dict, *, check_only: bool = False, root: Path | None = None) -> bool:
    """Install one backend set. Returns True when it is present and verified."""
    pkg = root or llama_cpp_lib_root()
    target = pkg / spec["target_dir"]

    if verify_backend(target, spec):
        print(f"  {name}: verified installed payload at {target}")
        return True

    if check_only:
        print(f"  {name}: MISSING at {target}")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        wheel = Path(tmp) / "backend.whl"
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

        staged = Path(tmp) / "payload"
        staged.mkdir()
        with zipfile.ZipFile(wheel) as zf:
            for member in zf.infolist():
                filename = member.filename
                if member.is_dir() or filename.endswith(".lib"):
                    continue
                if filename.startswith("llama_cpp/lib/"):
                    relative = filename if spec.get("layout") == "wheel" else filename.removeprefix("llama_cpp/lib/")
                elif spec.get("layout") == "wheel" and filename.startswith("llama_cpp_python.libs/"):
                    relative = filename
                elif filename.endswith(".dist-info/licenses/LICENSE.md"):
                    relative = "LICENSE.llama-cpp-python.md"
                else:
                    continue
                rel = PurePosixPath(relative)
                if rel.is_absolute() or ".." in rel.parts or "\\" in relative:
                    raise SystemExit(f"{name}: unsafe wheel member")
                dest = staged / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, dest.open("wb") as output:
                    shutil.copyfileobj(source, output)

        payload = {f.relative_to(staged).as_posix(): _sha256(f) for f in staged.rglob("*") if f.is_file()}
        (staged / _MARKER).write_text(
            json.dumps(
                {
                    "backend": name,
                    "wrapper_version": spec.get("wrapper_version", WRAPPER_VERSION),
                    "llama_cpp_commit": spec.get("llama_cpp_commit", LLAMA_CPP_COMMIT),
                    "url": spec["url"],
                    "sha256": spec["sha256"],
                    "size": spec["size"],
                    "files": payload,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if not verify_backend(staged, spec):
            raise SystemExit(f"{name}: incomplete backend payload")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staged, target)
        total_mb = sum((target / name).stat().st_size for name in payload) / (1024 * 1024)
        print(f"  {name}: installed {len(payload)} libraries ({total_mb:.1f} MB) to {target}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", help="install only this backend (default: all for this platform)")
    parser.add_argument("--root", type=Path, help="verify backend sets in a packaged llama_cpp directory (requires --check)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is installed and exit non-zero if anything is missing",
    )
    args = parser.parse_args()

    artifacts = platform_artifacts()
    if args.root and not args.check:
        parser.error("--root requires --check")
    if not artifacts:
        print(f"No extra llama.cpp backend sets are defined for {sys.platform}; nothing to do.")
        return 0

    wanted = artifacts
    if args.backend:
        if args.backend not in artifacts:
            print(
                f"error: no {args.backend!r} backend for {sys.platform}; "
                f"known: {sorted(artifacts)}",
                file=sys.stderr,
            )
            return 2
        wanted = {args.backend: artifacts[args.backend]}

    print(f"llama.cpp backend sets for {sys.platform}:")
    ok = True
    for name, spec in wanted.items():
        if not args.root:
            assert_wrapper_version(spec.get("wrapper_version", WRAPPER_VERSION))
        ok = install_backend(name, spec, check_only=args.check, root=args.root) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
