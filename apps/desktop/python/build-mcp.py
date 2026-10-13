"""PyInstaller build driver for the potluck-mcp binary.

Produces a onedir bundle at dist/potluck-mcp/ containing the executable
and an _internal/ directory with all shared libraries and data files.

Heavy inference deps (llama_cpp, torch, sentence_transformers, numpy, GPUtil)
are excluded - the MCP server only needs the db/settings/services layer.

The spec file (potluck-mcp.spec) is the source of truth for build flags.
Regenerate it intentionally when dependencies change - do not delete it
between builds, as that breaks reproducibility.

Usage:
    python build-mcp.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent


def main() -> None:
    spec = _ROOT / "potluck-mcp.spec"
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

    exe_name = "potluck-mcp.exe" if sys.platform == "win32" else "potluck-mcp"
    binary = _ROOT / "dist" / "potluck-mcp" / exe_name
    print(f"\nBuild complete: {binary}")
    print(f"Bundle directory: {binary.parent}")


if __name__ == "__main__":
    main()
