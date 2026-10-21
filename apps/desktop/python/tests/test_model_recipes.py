"""Recipe provenance, live resource gates and read-only artifact checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from potluck_local.services import model_recipes as mr

GIB = 1024 ** 3
ARTIFACT = b"GGUF recipe fixture"


@pytest.fixture
def recipe():
    return {
        "id": "small-cpu-4k", "name": "Small CPU recipe",
        "model": {
            "id": "small.q4", "repository": "Qwen/Small-GGUF", "revision": "a" * 40,
            "filename": "small-q4.gguf", "sha256": hashlib.sha256(ARTIFACT).hexdigest(),
            "size_bytes": len(ARTIFACT),
        },
        "engine": {
            "backend": "llama-server", "commit": mr.PIN["commit"], "acceleration": "cpu",
            "platforms": [{"os": "Darwin", "arch": "arm64"}],
        },
        "runtime": {"context_size": 4096, "gpu_layers": 0, "native_cache_mib": 128},
        "resources": {"kv_bytes_per_token": 12288, "overhead_bytes": GIB},
        "qualification": "experimental", "notes": "Estimates only.",
    }


@pytest.fixture
def host(monkeypatch, tmp_path):
    base = tmp_path / "not-created" / "models"
    monkeypatch.setattr(mr, "models_dir", lambda: base)
    monkeypatch.setattr(mr.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(mr.platform, "machine", lambda: "arm64")
    state = {"ram": 64 * GIB, "disk": 128 * GIB, "backend": "metal"}
    monkeypatch.setattr(mr.psutil, "virtual_memory", lambda: SimpleNamespace(available=state["ram"]))
    monkeypatch.setattr(mr.shutil, "disk_usage", lambda path: SimpleNamespace(free=state["disk"]))
    monkeypatch.setattr(mr, "engine_bundle", lambda: (
        tmp_path / "engine", {"backend": state["backend"], "commit": mr.PIN["commit"]}
    ))
    return base, state


def install(recipe):
    path = mr.recipe_path(recipe)
    path.parent.mkdir(parents=True)
    path.write_bytes(ARTIFACT)
    return path


def codes(result):
    return {issue["code"] for issue in result["issues"]}


@pytest.mark.parametrize("path,value", [
    (("model", "revision"), "main"),
    (("model", "revision"), "a" * 39),
    (("model", "sha256"), "a" * 63),
    (("model", "sha256"), "A" * 64),
    (("model", "repository"), "https://evil.invalid/model"),
    (("model", "repository"), "Qwen/../secret"),
    (("model", "filename"), "../secret.gguf"),
    (("model", "filename"), "small-00001-of-00002.gguf"),
    (("model", "filename"), "small.gguf?token=secret"),
    (("model", "id"), ".."),
    (("model", "id"), "a/b"),
    (("model", "id"), "a\\b"),
    (("model", "size_bytes"), 0),
    (("model", "size_bytes"), "1024"),
    (("model", "size_bytes"), True),
    (("runtime", "context_size"), 512),
    (("runtime", "context_size"), 131073),
    (("runtime", "native_cache_mib"), 4097),
    (("runtime", "gpu_layers"), -1),
    (("runtime", "gpu_layers"), False),
    (("runtime", "gpu_layers"), 0.0),
    (("resources", "kv_bytes_per_token"), 0),
    (("resources", "overhead_bytes"), -1),
    (("engine", "backend"), "llama-cpp-python"),
    (("qualification",), "qualified"),
    (("download_url",), "https://evil.invalid/model.gguf"),
])
def test_schema_rejects_mutable_unbounded_coerced_or_unsafe_fields(recipe, path, value):
    target = recipe
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        mr.ModelRecipe.model_validate(recipe)


def test_schema_rejects_duplicate_ids_platforms_and_acceleration_mismatches(recipe):
    with pytest.raises(ValidationError, match="unique"):
        mr.RecipeCatalog.model_validate({"schema_version": 1, "recipes": [recipe, recipe]})
    recipe["engine"]["platforms"].append(recipe["engine"]["platforms"][0])
    with pytest.raises(ValidationError, match="unique"):
        mr.ModelRecipe.model_validate(recipe)
    recipe["engine"]["platforms"] = [{"os": "Linux", "arch": "x86_64"}]
    recipe["engine"]["acceleration"] = "metal"
    with pytest.raises(ValidationError, match="Apple Silicon"):
        mr.ModelRecipe.model_validate(recipe)


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_catalog_version_is_strict(recipe, version):
    with pytest.raises(ValidationError):
        mr.RecipeCatalog.model_validate({"schema_version": version, "recipes": [recipe]})


def test_load_uses_only_bundled_pins_returns_copies_and_never_probes(recipe, tmp_path, monkeypatch):
    path = tmp_path / "model-recipes.json"
    path.write_text(json.dumps({"schema_version": 1, "recipes": [recipe]}))
    monkeypatch.setattr(mr.importlib.resources, "files", lambda name: tmp_path)
    monkeypatch.setattr(mr, "engine_bundle", lambda: pytest.fail("Listing must not probe hardware"))
    result = mr.load_recipes()
    assert result == [recipe]
    result[0]["model"]["revision"] = "mutated"
    assert mr.get_recipe(recipe["id"]) == recipe
    assert mr.get_recipe("unknown") is None
    assert mr.recipe_url(recipe) == (
        "https://huggingface.co/Qwen/Small-GGUF/resolve/" + "a" * 40 + "/small-q4.gguf"
    )


def test_missing_artifact_is_installable_without_creating_directory(recipe, host):
    base, _ = host
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["can_install"] and not result["can_load"]
    assert not result["installed"] and not result["artifact_verified"]
    assert codes(result) == {"artifact_missing"}
    assert not base.parent.exists()
    assert result["resources"]["required_disk_bytes"] == len(ARTIFACT) + mr.DISK_MARGIN_BYTES
    assert "qualification" in result["warnings"][0]


@pytest.mark.parametrize("present", [0, 7, len(ARTIFACT)])
def test_matching_partial_counts_only_remaining_download_without_writes(recipe, host, present):
    base, state = host
    directory = base / recipe["model"]["id"]
    directory.mkdir(parents=True)
    partial = directory / "model.gguf.partial"
    meta = directory / "model.gguf.partial.meta"
    partial.write_bytes(ARTIFACT[:present])
    meta.write_text(json.dumps({"url": mr.recipe_url(recipe), "sha256": recipe["model"]["sha256"]}))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (partial, meta)}
    required = len(ARTIFACT) - present + mr.DISK_MARGIN_BYTES
    state["disk"] = required
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["resources"]["required_disk_bytes"] == required
    assert result["can_install"] and not result["can_load"]
    assert not result["installed"] and not result["artifact_verified"]
    assert codes(result) == {"artifact_missing"}
    assert set(directory.iterdir()) == {partial, meta}
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (partial, meta)}
    state["disk"] -= 1
    assert "disk_insufficient" in codes(mr.preflight_recipe(recipe))


@pytest.mark.parametrize("problem", ["revision", "sha256", "oversized", "missing_meta", "invalid_json", "nonobject_json"])
def test_untrusted_partial_cannot_reduce_disk_requirement(recipe, host, problem):
    base, state = host
    directory = base / recipe["model"]["id"]
    directory.mkdir(parents=True)
    partial = directory / "model.gguf.partial"
    meta = directory / "model.gguf.partial.meta"
    partial.write_bytes(ARTIFACT + b"extra" if problem == "oversized" else ARTIFACT[:7])
    metadata = {"url": mr.recipe_url(recipe), "sha256": recipe["model"]["sha256"]}
    if problem == "revision":
        metadata["url"] = metadata["url"].replace("a" * 40, "b" * 40)
    elif problem == "sha256":
        metadata["sha256"] = "b" * 64
    if problem != "missing_meta":
        meta.write_text("invalid" if problem == "invalid_json" else "[]" if problem == "nonobject_json" else json.dumps(metadata))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    state["disk"] = mr.DISK_MARGIN_BYTES + len(ARTIFACT) - 1
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["resources"]["required_disk_bytes"] == len(ARTIFACT) + mr.DISK_MARGIN_BYTES
    assert not result["can_install"] and not result["can_load"]
    assert codes(result) == {"artifact_missing", "disk_insufficient"}
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in directory.iterdir()}


def test_exact_verified_artifact_can_load_without_settings_or_metadata_writes(recipe, host):
    base, _ = host
    path = install(recipe)
    before = path.stat()
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["can_install"] and result["can_load"]
    assert result["installed"] and result["artifact_verified"]
    assert result["issues"] == []
    assert result["resources"]["required_disk_bytes"] == 0
    assert list(base.rglob("*")) == [path.parent, path]
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert result["recipe"] == recipe


def test_installed_artifact_without_hash_probe_is_not_ready(recipe, host, monkeypatch):
    install(recipe)
    monkeypatch.setattr(mr, "artifact_identity", lambda path: pytest.fail("Unexpected hash read"))
    result = mr.preflight_recipe(recipe)
    assert result["installed"] and not result["artifact_verified"]
    assert not result["can_load"] and not result["can_install"]
    assert codes(result) == {"artifact_unverified"}


@pytest.mark.parametrize("different", [b"short", b"X" * len(ARTIFACT)])
def test_mismatched_existing_artifact_never_overwritten_or_accepted(recipe, host, different):
    path = install(recipe)
    path.write_bytes(different)
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert not result["can_install"] and not result["can_load"]
    assert not result["artifact_verified"]
    assert codes(result) == {"artifact_size_mismatch" if len(different) != len(ARTIFACT) else "artifact_hash_mismatch"}
    assert path.read_bytes() == different


def test_hash_read_failure_or_file_change_during_hash_blocks_load(recipe, host, monkeypatch):
    install(recipe)
    def changed(path):
        raise OSError("changed while hashing")
    monkeypatch.setattr(mr, "artifact_identity", changed)
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert not result["can_load"] and not result["can_install"]
    assert codes(result) == {"artifact_unreadable"}


@pytest.mark.parametrize("target", ["directory", "file", "broken_file", "occupied_directory", "occupied_base"])
def test_symlinks_and_non_directory_paths_are_rejected(recipe, host, tmp_path, target):
    base, _ = host
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    artifact = outside / "model.gguf"
    artifact.write_bytes(ARTIFACT)
    directory = base / recipe["model"]["id"]
    if target == "directory":
        directory.symlink_to(outside, target_is_directory=True)
    elif target == "occupied_directory":
        directory.write_bytes(b"occupied")
    elif target == "occupied_base":
        base.rmdir()
        base.write_bytes(b"occupied")
    else:
        directory.mkdir()
        (directory / "model.gguf").symlink_to(artifact if target == "file" else outside / "missing")
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert codes(result) == {"artifact_path_invalid"}
    assert not result["can_load"] and not result["can_install"]
    assert artifact.read_bytes() == ARTIFACT


@pytest.mark.parametrize("name", ["model.gguf.partial", "model.gguf.partial.meta", "model.gguf.sha256"])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_download_support_paths_cannot_redirect_or_block_writes(recipe, host, tmp_path, name, kind):
    path = install(recipe)
    outside = tmp_path / "outside"
    outside.write_bytes(b"private content")
    support = path.parent / name
    if kind == "symlink":
        support.symlink_to(outside)
    else:
        support.mkdir()
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert codes(result) == {"artifact_path_invalid"}
    assert not result["can_load"] and not result["can_install"]
    assert outside.read_bytes() == b"private content"


def test_artifact_symlink_loop_fails_without_following_it(recipe, host):
    path = install(recipe)
    path.unlink()
    path.symlink_to(path)
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert codes(result) == {"artifact_path_invalid"}
    assert not result["can_load"] and not result["can_install"]


@pytest.mark.parametrize("backend", ["cpu", "metal", "cuda"])
def test_cpu_recipe_accepts_any_pinned_engine_without_double_counting_vram(recipe, host, backend):
    _, state = host
    state["backend"] = backend
    install(recipe)
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["can_load"]
    resources = result["resources"]
    assert resources["estimated_ram_bytes"] == len(ARTIFACT) + 4096 * 12288 + 128 * mr.MIB + GIB
    assert "available_vram_bytes" not in resources and "estimated_vram_bytes" not in resources


def test_metal_checks_one_live_unified_ram_pool(recipe, host):
    recipe["engine"]["acceleration"] = "metal"
    recipe["runtime"]["gpu_layers"] = -1
    install(recipe)
    _, state = host
    estimate = len(ARTIFACT) + 4096 * 12288 + 128 * mr.MIB + GIB
    state["ram"] = estimate
    assert mr.preflight_recipe(recipe, verify_artifact=True)["can_load"]
    state["ram"] -= 1
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert codes(result) == {"ram_insufficient"}
    assert result["can_install"] and not result["can_load"]


def test_runtime_and_platform_failures_only_block_loading(recipe, host, monkeypatch):
    monkeypatch.setattr(mr.platform, "system", lambda: "Windows")
    recipe["engine"]["commit"] = "f" * 40
    def missing():
        raise mr.SplitError("missing")
    monkeypatch.setattr(mr, "engine_bundle", missing)
    result = mr.preflight_recipe(recipe)
    assert codes(result) == {"platform_unsupported", "engine_commit_mismatch", "engine_missing", "artifact_missing"}
    assert result["can_install"] and not result["can_load"]


def test_accelerated_recipe_requires_its_exact_bundle_backend(recipe, host):
    recipe["engine"]["acceleration"] = "metal"
    recipe["runtime"]["gpu_layers"] = -1
    install(recipe)
    _, state = host
    state["backend"] = "cpu"
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert codes(result) == {"engine_acceleration_mismatch"}
    assert not result["can_load"]


def test_live_ram_and_disk_are_reprobed_each_call(recipe, host):
    _, state = host
    assert mr.preflight_recipe(recipe)["can_install"]
    state.update(ram=0, disk=0)
    result = mr.preflight_recipe(recipe)
    assert codes(result) == {"ram_insufficient", "disk_insufficient", "artifact_missing"}
    assert not result["can_install"] and not result["can_load"]


@pytest.mark.parametrize("field,value", [("ram", None), ("ram", -1), ("ram", True), ("disk", None), ("disk", -1)])
def test_unknown_or_invalid_resource_readings_fail_closed(recipe, host, field, value):
    _, state = host
    state[field] = value
    result = mr.preflight_recipe(recipe)
    assert field + "_unknown" in codes(result)
    assert not result["can_load" if field == "ram" else "can_install"]


def cuda_recipe(recipe, monkeypatch):
    recipe["engine"].update(acceleration="cuda", platforms=[{"os": "Linux", "arch": "x86_64"}])
    recipe["runtime"]["gpu_layers"] = -1
    monkeypatch.setattr(mr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(mr.platform, "machine", lambda: "x86_64")
    return recipe


@pytest.mark.parametrize("free,expected", [(None, "vram_unknown"), (0, "vram_insufficient"), (64 * GIB, None)])
def test_cuda_requires_live_free_vram_not_total_capacity(recipe, host, monkeypatch, free, expected):
    cuda_recipe(recipe, monkeypatch)
    host[1]["backend"] = "cuda"
    install(recipe)
    monkeypatch.setattr(mr, "_cuda_free", lambda root: free)
    result = mr.preflight_recipe(recipe, verify_artifact=True)
    assert result["can_load"] == (expected is None)
    assert codes(result) == ({expected} if expected else set())
    assert result["resources"]["available_vram_bytes"] == free
    assert result["resources"]["estimated_vram_bytes"] == len(ARTIFACT) + 4096 * 12288 + GIB


def test_cuda_native_probe_does_not_load_model_and_uses_smallest_visible_free_pool(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=(
            b"Available devices:\nCUDA0: First (24000 MiB, 18000 MiB free)\n"
            b"CUDA1: Second (24000 MiB, 4000 MiB free)\n"
        ))
    monkeypatch.setattr(mr.subprocess, "run", run)
    monkeypatch.setenv("LLAMA_ARG_MODEL", "/private/model.gguf")
    monkeypatch.setenv("GGML_RPC_ENDPOINT", "external.invalid")
    assert mr._cuda_free(Path("/pinned")) == 4000 * mr.MIB
    args, kwargs = calls[0]
    assert args == ["/pinned/llama-server", "--list-devices"]
    assert not any(key.startswith(("LLAMA_", "GGML_RPC_")) for key in kwargs["env"])
    assert kwargs["timeout"] == 10


@pytest.mark.parametrize("output", [
    b"CUDA0: GPU (24000 MiB)",
    b"CUDA0: GPU (24000 MiB, unknown MiB free)",
    b"CUDA0: GPU (24000 MiB, 25000 MiB free)",
    b"MTL0: Apple (24000 MiB, 18000 MiB free)",
    b"CUDA0: GPU (24000 MiB, 18000 MiB free)\nCUDA1: GPU (24000 MiB)",
    b"x" * 16385,
])
def test_cuda_inventory_unknown_readings_never_become_free_memory(monkeypatch, output):
    monkeypatch.setattr(mr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output))
    assert mr._cuda_free(Path("/pinned")) is None


def test_packaged_recipes_validate_and_have_no_qualified_claims():
    recipes = mr.load_recipes()
    assert recipes
    for recipe in recipes:
        assert recipe["qualification"] == "experimental"
        assert recipe["engine"]["commit"] == mr.PIN["commit"]
        assert "/resolve/main/" not in mr.recipe_url(recipe)
