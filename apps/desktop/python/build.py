"""PyInstaller build driver.

Produces a single self-contained binary at dist/potluck-local[.exe].
Code signing and notarization are handled in a later CI task.

Usage:
    python build.py
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
        "--name", "potluck-local",
        "--clean",
        # Bundle the offline model registry fallback
        "--add-data", f"{_ROOT / 'potluck_local' / 'data' / 'models.json'}:potluck_local/data",
        str(_ROOT / "potluck_local" / "app.py"),
    ]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=_ROOT)

    if result.returncode != 0:
        sys.exit(result.returncode)

    binary = _ROOT / "dist" / "potluck-local"
    if sys.platform == "win32":
        binary = binary.with_suffix(".exe")

    print(f"\nBuild complete: {binary}")


if __name__ == "__main__":
    main()
