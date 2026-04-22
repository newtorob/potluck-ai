"""PyInstaller build driver for the potluck-mcp binary.

Produces a single self-contained binary at dist/potluck-mcp[.exe].
Heavy inference dependencies (llama_cpp, torch, sentence_transformers) are
explicitly excluded — the MCP server only needs the db/settings/services layer.

Usage:
    python build-mcp.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent


def main() -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name", "potluck-mcp",
        "--clean",
        # Exclude heavy inference deps — MCP does not call the LLM
        "--exclude-module", "llama_cpp",
        "--exclude-module", "torch",
        "--exclude-module", "sentence_transformers",
        "--exclude-module", "numpy",
        "--exclude-module", "GPUtil",
        # Entry point is the __main__ module
        str(_ROOT / "potluck_local" / "mcp" / "__main__.py"),
    ]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=_ROOT)

    if result.returncode != 0:
        sys.exit(result.returncode)

    binary = _ROOT / "dist" / "potluck-mcp"
    if sys.platform == "win32":
        binary = binary.with_suffix(".exe")

    print(f"\nBuild complete: {binary}")


if __name__ == "__main__":
    main()
