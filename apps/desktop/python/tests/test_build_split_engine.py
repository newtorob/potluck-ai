import importlib.util
import io
from pathlib import Path
import sys
import tarfile

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "build-split-engine.py"


@pytest.fixture
def build(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("build_split_engine", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    header = "".join(f"#define RPC_PROTO_{k}_VERSION {v}\n" for k, v in zip(("MAJOR", "MINOR", "PATCH"), module.PIN["rpcProtocol"].split(".")))
    calls = []

    def run(command, stdout=None, check=False):
        calls.append(command)
        if command[0] == "git":
            with tarfile.open(fileobj=stdout, mode="w") as tar:
                for name, text in (("ggml/include/ggml-rpc.h", header), ("LICENSE", "license")):
                    info = tarfile.TarInfo(name)
                    info.size = len(text)
                    tar.addfile(info, io.BytesIO(text.encode()))
        elif "--build" in command:
            built = Path(command[command.index("--build") + 1]) / "bin"
            built = built / "Release" if sys.platform == "win32" else built
            built.mkdir(parents=True)
            suffix = ".exe" if sys.platform == "win32" else ""
            for name in ("llama-server", "ggml-rpc-server"):
                (built / (name + suffix)).write_text(name)
    monkeypatch.setattr(module.subprocess, "run", run)

    def go(backend, platform, name="bundle"):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(sys, "argv", ["build", "--source", str(tmp_path), "--backend", backend, "--output", str(tmp_path / name)])
        module.main()
        return tmp_path / name
    return go, calls


def test_vulkan_backend_turns_on_only_vulkan(build):
    go, calls = build
    output = go("vulkan", "linux")
    configure = next(c for c in calls if c[0] == "cmake" and "-S" in c)
    assert "-DGGML_VULKAN=ON" in configure and "-DGGML_CUDA=OFF" in configure and "-DGGML_METAL=OFF" in configure
    assert sorted(p.name for p in output.iterdir()) == ["LICENSE", "llama-server", "manifest.json", "rpc-server"]
    assert '"backend": "vulkan"' in (output / "manifest.json").read_text()
    assert (output / "rpc-server").read_text() == "ggml-rpc-server"


def test_other_backends_leave_vulkan_off(build):
    go, calls = build
    go("cuda", "linux")
    assert "-DGGML_VULKAN=OFF" in next(c for c in calls if "-S" in c)


def test_windows_build_copies_release_exe_names(build):
    go, _ = build
    output = go("vulkan", "win32")
    assert sorted(p.name for p in output.iterdir()) == ["LICENSE", "llama-server.exe", "manifest.json", "rpc-server.exe"]
    assert (output / "rpc-server.exe").read_text() == "ggml-rpc-server"


def test_existing_output_is_still_refused(build, tmp_path):
    go, _ = build
    (tmp_path / "taken").mkdir()
    with pytest.raises(SystemExit):
        go("vulkan", "linux", "taken")
