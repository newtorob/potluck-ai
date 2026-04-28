#!/usr/bin/env python3
"""One-shot contributor sidecar installer.

Cross-platform: macOS / Linux / Windows. Run this from a checked-out
copy of the repo after `git clone`. Idempotent — safe to re-run.

What it does:
    1. Verifies Python 3.11+
    2. Creates apps/desktop/python/.venv if missing
    3. Installs requirements-contributor.txt (slim — no llama-cpp-python)
    4. pip-installs the potluck-local package in editable mode
    5. Verifies the `potluck-contributor` entry point is reachable
    6. Writes a `contributor.env.example` you can copy to your shell
    7. Prints the next-step run command for your platform

What it does NOT do:
    - Install Python, git, or Tailscale (do that yourself first)
    - Clone the repo (you already did)
    - Write your real env vars (you set those before running the sidecar)
    - Generate the per-peer Ed25519 key (sidecar does that on first run)
    - Modify anything outside apps/desktop/python/

Usage:
    python3 scripts/install-contributor.py            # install + print next step
    python3 scripts/install-contributor.py --start    # install then launch
    python3 scripts/install-contributor.py --reset    # delete venv first
    python3 scripts/install-contributor.py --quiet    # less output
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


MIN_PY = (3, 11)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sidecar_dir() -> Path:
    return repo_root() / "apps" / "desktop" / "python"


def venv_dir() -> Path:
    return sidecar_dir() / ".venv"


def venv_bin() -> Path:
    """Where venv-managed executables live (cross-platform)."""
    if sys.platform == "win32":
        return venv_dir() / "Scripts"
    return venv_dir() / "bin"


def venv_python() -> Path:
    return venv_bin() / ("python.exe" if sys.platform == "win32" else "python")


def venv_pip() -> Path:
    return venv_bin() / ("pip.exe" if sys.platform == "win32" else "pip")


def contributor_entry() -> Path:
    return venv_bin() / (
        "potluck-contributor.exe" if sys.platform == "win32" else "potluck-contributor"
    )


def fail(msg: str, code: int = 1) -> None:
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


def check_python_version() -> None:
    if sys.version_info < MIN_PY:
        fail(
            f"need Python {MIN_PY[0]}.{MIN_PY[1]}+, got "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
            f"  install:  brew install python@3.11   (macOS)\n"
            f"            sudo apt install python3.11 python3.11-venv  (Debian/Ubuntu)\n"
            f"            winget install Python.Python.3.11  (Windows)\n"
            f"  then re-run this script with the newer python: "
            f"python3.11 {sys.argv[0]}"
        )


def run(cmd: list[str], cwd: Path | None = None, quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=None,
    )
    if result.returncode != 0:
        fail(f"command failed (exit {result.returncode}): {' '.join(map(str, cmd))}")


def create_venv(quiet: bool) -> None:
    print(f"==> Creating venv at {venv_dir()}")
    run([sys.executable, "-m", "venv", str(venv_dir())], quiet=quiet)


def install_deps(quiet: bool) -> None:
    print("==> Upgrading pip")
    run([str(venv_pip()), "install", "--upgrade", "--quiet", "pip"], quiet=quiet)
    print("==> Installing contributor dependencies (slim — no llama-cpp-python)")
    req = sidecar_dir() / "requirements-contributor.txt"
    if not req.exists():
        fail(f"missing requirements file: {req}")
    run([str(venv_pip()), "install", "-r", str(req)], quiet=quiet)
    print("==> Installing potluck-local package (editable)")
    run([str(venv_pip()), "install", "-e", str(sidecar_dir())], quiet=quiet)


def verify_entry_point() -> None:
    entry = contributor_entry()
    if not entry.exists():
        fail(
            f"entry point not found at {entry}.\n"
            f"  this usually means `pip install -e .` did not register the script.\n"
            f"  try removing the venv and re-running with --reset."
        )
    print(f"==> Entry point OK: {entry}")


def write_env_example() -> Path:
    template = (
        "# Copy this file to .env and edit the placeholders below.\n"
        "# The sidecar auto-loads .env from its working directory.\n"
        "#\n"
        "# Find the dev Mac's LAN IP:\n"
        "#   macOS:    ipconfig getifaddr en0\n"
        "#   Linux:    hostname -I | awk '{print $1}'\n"
        "#   Windows:  ipconfig | findstr IPv4\n"
        "# Or its Tailscale IP if you're on a different network: tailscale ip -4\n"
        "\n"
        "# Pick a hash that exists in coordinator.contributor_releases:\n"
        "POTLUCK_BINARY_HASH=59b74f2ea0e8b6f23f3672336f90c97435b1dfa13ef7aabc1994b61c54c7d65a\n"
        "\n"
        "# REPLACE THIS with the dev Mac's actual IP — the placeholder will not work:\n"
        "POTLUCK_COORDINATOR_URL=ws://REPLACE_WITH_DEV_MAC_IP:8400/peers/connect\n"
        "\n"
        "# Dev member fixture (any UUID works for m1):\n"
        "POTLUCK_MEMBER_ID=00000000-0000-0000-0000-000000000001\n"
    )
    target = sidecar_dir() / "contributor.env.example"
    target.write_text(template)
    print(f"==> Wrote env template: {target}")
    return target


def print_next_steps(env_example: Path) -> None:
    entry = contributor_entry()
    if sys.platform == "win32":
        run_cmd = (
            f"$env:POTLUCK_BINARY_HASH = '<hash>'\n"
            f"$env:POTLUCK_COORDINATOR_URL = 'ws://<dev-mac-ip>:8400/peers/connect'\n"
            f"$env:POTLUCK_MEMBER_ID = '00000000-0000-0000-0000-000000000001'\n"
            f"{entry} --verbose"
        )
    else:
        run_cmd = (
            f"POTLUCK_BINARY_HASH=<hash> \\\n"
            f"POTLUCK_COORDINATOR_URL=ws://<dev-mac-ip>:8400/peers/connect \\\n"
            f"POTLUCK_MEMBER_ID=00000000-0000-0000-0000-000000000001 \\\n"
            f"{entry} --verbose"
        )

    print()
    print("=" * 64)
    print("Install complete. Next steps:")
    print("=" * 64)
    print(f"\n1. The template at {env_example.name} is regenerated on each install —")
    print(f"   don't edit it in place. Copy then edit:")
    print(f"\n   cp {env_example} {env_example.parent / '.env'}")
    print(f"   # then edit {env_example.parent / '.env'} with your dev Mac's IP")
    print(f"\n2. Run the sidecar (auto-loads {env_example.parent / '.env'}):")
    print(f"\n   cd {sidecar_dir()}")
    print(f"   {contributor_entry()} --verbose")
    print(f"\n   ...or pass values inline:")
    print(f"\n{run_cmd}\n")
    print("3. Verify from another machine:")
    print("\n   curl http://<dev-mac-ip>:8400/transparency/fleet\n")
    print("Full doc: docs/phase-g/lan-smoke-setup.md")


def reset_venv() -> None:
    if venv_dir().exists():
        print(f"==> --reset: removing {venv_dir()}")
        shutil.rmtree(venv_dir())


def launch_sidecar() -> None:
    """Replace this process with the sidecar so Ctrl-C stops it cleanly."""
    print(f"==> --start: launching {contributor_entry()}")
    print("    (set POTLUCK_BINARY_HASH / POTLUCK_COORDINATOR_URL / "
          "POTLUCK_MEMBER_ID first, or this will exit immediately)")
    print()
    if sys.platform == "win32":
        # os.execv on Windows has caveats with subprocess inheritance.
        # subprocess.run is more predictable.
        result = subprocess.run([str(contributor_entry()), "--verbose"])
        sys.exit(result.returncode)
    else:
        os.execv(str(contributor_entry()), [str(contributor_entry()), "--verbose"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", action="store_true", help="launch the sidecar after install")
    p.add_argument("--reset", action="store_true", help="delete .venv before installing")
    p.add_argument("--quiet", action="store_true", help="less verbose pip output")
    args = p.parse_args()

    check_python_version()

    if not sidecar_dir().exists():
        fail(
            f"sidecar dir not found: {sidecar_dir()}\n"
            f"  this script must be run from inside a checked-out copy of the repo."
        )

    if args.reset:
        reset_venv()

    if not venv_dir().exists():
        create_venv(args.quiet)
    else:
        print(f"==> Reusing existing venv at {venv_dir()}")

    install_deps(args.quiet)
    verify_entry_point()
    env_example = write_env_example()

    if args.start:
        launch_sidecar()
    else:
        print_next_steps(env_example)


if __name__ == "__main__":
    main()
