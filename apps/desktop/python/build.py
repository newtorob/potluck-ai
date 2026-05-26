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


def main() -> None:
    spec = _ROOT / "potluck-local.spec"
    if not spec.exists():
        print(f"error: spec file not found: {spec}", file=sys.stderr)
        print("Run PyInstaller manually once to generate it, then commit it.", file=sys.stderr)
        sys.exit(1)

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


if __name__ == "__main__":
    main()
