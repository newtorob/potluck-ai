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

    Windows and Linux x86-64 use a CPU fallback wheel and a matching Vulkan
    set. No-op on platforms without additional packaged backends.
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
        count = len(list(backend_dir.rglob("*.dll"))) + len(list(backend_dir.rglob("*.so*")))
        print(f"  backend set bundled: {backend_dir.name} ({count} libraries)")
    # Validate every recorded library, including Linux's hashed auditwheel
    # dependencies. An empty directory or a surviving marker is not enough.
    result = subprocess.run([
        sys.executable, str(_ROOT / "scripts" / "vendor_gpu_backend.py"),
        "--check", "--root", str(lib_root),
    ], cwd=_ROOT)
    if result.returncode:
        sys.exit(result.returncode)

    # Stamp the build identity the contributor presents to the coordinator as
    # binary_hash: SHA-256 over the unsigned PyInstaller output, written to
    # contributor-release.json inside the bundle (see contributor/release.py).
    # Computed here, before code signing and before electron-builder re-signs
    # nested binaries, because signing rewrites Mach-O bytes and a file added
    # after signing would break the app seal. So this identifies the build
    # inputs, which is what the coordinator's release allowlist keys on; it
    # is not a tamper check of the shipped bytes (that is the OS signature).
    sys.path.insert(0, str(_ROOT))
    from potluck_local.contributor.release import write_release_info

    info = write_release_info(binary.parent, _release_version())
    print(f"Release stamp: version={info['version']} binary_hash={info['binary_hash'][:16]}…")


def _release_version() -> str:
    """The desktop release number (apps/desktop/electron/package.json), which
    is what a signed release row would carry; falls back to the sidecar's
    own __version__ when the Electron manifest is not next to us."""
    import json

    manifest = _ROOT.parent / "electron" / "package.json"
    try:
        version = json.loads(manifest.read_text()).get("version")
        if isinstance(version, str) and version:
            return version
    except (OSError, ValueError):
        pass
    from potluck_local import __version__

    return __version__


if __name__ == "__main__":
    main()
