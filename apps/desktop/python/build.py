"""PyInstaller build driver for the potluck-local sidecar.

Produces a onedir bundle at dist/potluck-local/ containing the executable
and an _internal/ directory with all shared libraries and data files.

The spec file (potluck-local.spec) is the source of truth for build flags.
Regenerate it intentionally when dependencies change — do not delete it
between builds, as that breaks reproducibility.

Usage:
    python build.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent


def _vendor_gpu_backends() -> None:
    """Put the GPU backend set beside the CPU one before PyInstaller collects it.

    On Windows the wheel from requirements.txt is CPU only, so without this the
    bundle ships a build that cannot offload to any GPU. No-op elsewhere.
    """
    script = _ROOT / "scripts" / "vendor_gpu_backend.py"
    result = subprocess.run([sys.executable, str(script)], cwd=_ROOT)
    if result.returncode != 0:
        print(
            "error: could not install the GPU backend set; refusing to build a "
            "bundle that would silently be CPU only",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def main() -> None:
    spec = _ROOT / "potluck-local.spec"
    if not spec.exists():
        print(f"error: spec file not found: {spec}", file=sys.stderr)
        print("Run PyInstaller manually once to generate it, then commit it.", file=sys.stderr)
        sys.exit(1)

    _vendor_gpu_backends()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(spec),
        "--noconfirm",
    ]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=_ROOT)

    if result.returncode != 0:
        sys.exit(result.returncode)

    exe_name = "potluck-local.exe" if sys.platform == "win32" else "potluck-local"
    binary = _ROOT / "dist" / "potluck-local" / exe_name
    print(f"\nBuild complete: {binary}")
    print(f"Bundle directory: {binary.parent}")

    # A bundle whose GPU backend did not make it through collection is a
    # CPU-only release that looks fine. Say so at build time, not in a
    # playtester's five-minute timeout.
    lib_root = binary.parent / "_internal" / "llama_cpp"
    for backend_dir in sorted(lib_root.glob("lib-*")):
        count = len(list(backend_dir.glob("*.dll"))) + len(list(backend_dir.glob("*.so*")))
        print(f"  backend set bundled: {backend_dir.name} ({count} libraries)")
    if sys.platform == "win32" and not list(lib_root.glob("lib-*")):
        print(
            "error: no GPU backend set landed in the bundle; this build would be "
            "CPU only",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
