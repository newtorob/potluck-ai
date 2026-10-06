"""Exercise complete wheel extraction and the release gate with tiny artifacts."""
import hashlib
import importlib.util
import json
import shutil
import zipfile
from pathlib import Path

import pytest

_script = Path(__file__).parents[1] / "scripts" / "vendor_gpu_backend.py"
_spec = importlib.util.spec_from_file_location("vendor_gpu_backend", _script)
vendor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor)


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    path = tmp_path / "test.whl"
    members = {
        "llama_cpp/lib/libllama.so": b"llama",
        "llama_cpp/lib/libggml.so": b"ggml",
        "llama_cpp/lib/libggml-base.so": b"base",
        "llama_cpp/lib/libggml-vulkan.so": b"vulkan",
        "llama_cpp_python.libs/libggml-vulkan-hash.so.0": b"needed dependency",
        "llama_cpp_python-0.3.35.dist-info/licenses/LICENSE.md": b"license",
        "llama_cpp/__init__.py": b"must not replace wrapper",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    spec = {**vendor.ARTIFACTS["linux"]["vulkan"], "url": "https://example.invalid/backend.whl",
            "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    monkeypatch.setattr(vendor, "_download", lambda url, dst: shutil.copyfile(path, dst))
    root = tmp_path / "site-packages" / "llama_cpp"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "libllama.so").write_bytes(b"original CPU library")
    return root, spec, members


def test_linux_preserves_rpath_dependencies_and_cpu_payload(wheel):
    root, spec, members = wheel
    assert vendor.install_backend("vulkan", spec, root=root)
    target = root / "lib-vulkan"
    for name, content in members.items():
        if name.startswith(("llama_cpp/lib/", "llama_cpp_python.libs/")):
            assert (target / name).read_bytes() == content
    assert (root / "lib/libllama.so").read_bytes() == b"original CPU library"
    assert not (target / "llama_cpp/__init__.py").exists()
    assert vendor.install_backend("vulkan", spec, root=root, check_only=True)


@pytest.mark.parametrize("damage", ["delete", "corrupt", "dependency", "marker-only"])
def test_release_check_rejects_missing_or_corrupt_libraries(wheel, damage):
    root, spec, _ = wheel
    vendor.install_backend("vulkan", spec, root=root)
    target = root / "lib-vulkan"
    native = target / "llama_cpp/lib/libllama.so"
    if damage == "delete":
        native.unlink()
    elif damage == "corrupt":
        native.write_bytes(b"wrong")
    elif damage == "dependency":
        (target / "llama_cpp_python.libs/libggml-vulkan-hash.so.0").unlink()
    else:
        shutil.rmtree(target / "llama_cpp")
        shutil.rmtree(target / "llama_cpp_python.libs")
    assert not vendor.install_backend("vulkan", spec, root=root, check_only=True)


def test_bad_download_does_not_destroy_existing_backend(wheel):
    root, spec, _ = wheel
    vendor.install_backend("vulkan", spec, root=root)
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        vendor.install_backend("vulkan", {**spec, "sha256": "0" * 64}, root=root)
    assert vendor.verify_backend(root / "lib-vulkan", spec)


def test_check_does_not_trust_paths_outside_the_backend(wheel):
    root, spec, _ = wheel
    vendor.install_backend("vulkan", spec, root=root)
    target = root / "lib-vulkan"
    marker_path = target / vendor._MARKER
    marker = json.loads(marker_path.read_text())
    marker["files"]["../outside"] = "a" * 64
    marker_path.write_text(json.dumps(marker))
    assert not vendor.verify_backend(target, spec)


def test_linux_arm_does_not_install_x86_libraries(monkeypatch):
    monkeypatch.setattr(vendor.sys, "platform", "linux")
    monkeypatch.setattr(vendor.platform, "machine", lambda: "aarch64")
    assert vendor.platform_artifacts() == {}


def test_windows_flat_backend_still_installs(tmp_path, monkeypatch):
    wheel = tmp_path / "windows.whl"
    spec = dict(vendor.ARTIFACTS["win32"]["vulkan"])
    with zipfile.ZipFile(wheel, "w") as z:
        for name in spec["required"]:
            z.writestr("llama_cpp/lib/" + name, name.encode())
        z.writestr("llama_cpp/lib/llama.lib", b"link-time-only")
    spec.update(size=wheel.stat().st_size, sha256=hashlib.sha256(wheel.read_bytes()).hexdigest())
    monkeypatch.setattr(vendor, "_download", lambda url, dst: shutil.copyfile(wheel, dst))
    root = tmp_path / "llama_cpp"
    assert vendor.install_backend("vulkan", spec, root=root)
    assert (root / "lib-vulkan/llama.dll").is_file()
    assert not (root / "lib-vulkan/llama.lib").exists()
    assert vendor.verify_backend(root / "lib-vulkan", spec)
