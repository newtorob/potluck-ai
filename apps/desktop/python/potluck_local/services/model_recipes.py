"""Pinned local-engine recipes and read-only, live resource preflight.

A passing preflight checks declared requirements, not model quality or measured
performance. Recipes never inherit mutable catalog entries or acceptance proof.
"""
from __future__ import annotations

import importlib.resources
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Annotated, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from potluck_local.services.model_downloader import _partial_meta_matches
from potluck_local.services.model_identity import artifact_identity
from potluck_local.services.split_native import PIN, SplitError, engine_bundle
from potluck_local.utils.paths import models_dir

MIB = 1024 ** 2
DISK_MARGIN_BYTES = 256 * MIB
ESTIMATE_WARNING = (
    "Resource estimates are not model or hardware qualification, and do not establish "
    "answer quality, tool reliability or speed. Available resources can change before loading."
)
Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Commit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveBytes = Annotated[int, Field(gt=0, le=1024 ** 5)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RecipeModel(_StrictModel):
    id: Slug
    repository: str = Field(
        pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=256
    )
    revision: Commit
    filename: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$", max_length=256
    )
    sha256: Sha256
    size_bytes: PositiveBytes

    @model_validator(mode="after")
    def single_file_artifact(self):
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", self.filename):
            raise ValueError("Recipes require a complete single-file GGUF, not one shard")
        return self


class RecipePlatform(_StrictModel):
    os: Literal["Darwin", "Linux"]
    arch: Literal["arm64", "x86_64", "aarch64"]

    @model_validator(mode="after")
    def valid_platform(self):
        if self.os == "Darwin" and self.arch == "aarch64":
            raise ValueError("Darwin uses the arm64 architecture identifier")
        return self


class RecipeEngine(_StrictModel):
    backend: Literal["llama-server"]
    commit: Commit
    acceleration: Literal["cpu", "metal", "cuda"]
    platforms: list[RecipePlatform] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def valid_platforms(self):
        platforms = {(p.os, p.arch) for p in self.platforms}
        if len(platforms) != len(self.platforms):
            raise ValueError("Recipe platforms must be unique")
        if self.acceleration == "metal" and platforms != {("Darwin", "arm64")}:
            raise ValueError("Metal recipes require Apple Silicon")
        if self.acceleration == "cuda" and any(os != "Linux" for os, _ in platforms):
            raise ValueError("Native CUDA recipes require Linux")
        return self


class RecipeRuntime(_StrictModel):
    context_size: int = Field(ge=2048, le=131072)
    gpu_layers: Literal[0, -1]
    native_cache_mib: int = Field(ge=0, le=4096)

    @model_validator(mode="before")
    @classmethod
    def strict_layers(cls, value):
        # Literal integer values otherwise accept their equal boolean/float forms.
        if isinstance(value, dict) and type(value.get("gpu_layers")) is not int:
            raise ValueError("gpu_layers must be an integer")
        return value


class RecipeResources(_StrictModel):
    kv_bytes_per_token: int = Field(gt=0, le=1024 ** 3)
    overhead_bytes: PositiveBytes


class ModelRecipe(_StrictModel):
    id: Slug
    name: str = Field(min_length=1, max_length=200)
    model: RecipeModel
    engine: RecipeEngine
    runtime: RecipeRuntime
    resources: RecipeResources
    qualification: Literal["experimental"]
    notes: str = Field(max_length=4096)

    @model_validator(mode="after")
    def consistent_acceleration(self):
        expected_layers = 0 if self.engine.acceleration == "cpu" else -1
        if self.runtime.gpu_layers != expected_layers:
            raise ValueError("gpu_layers must match the recipe acceleration")
        return self


class RecipeCatalog(_StrictModel):
    schema_version: Literal[1]
    recipes: list[ModelRecipe] = Field(max_length=100)

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def unique_ids(self):
        if len({r.id for r in self.recipes}) != len(self.recipes):
            raise ValueError("Recipe IDs must be unique")
        return self


def load_recipes() -> list[dict]:
    """Read only bundled pins; no cloud catalog, proof store or hardware probe."""
    data = importlib.resources.files("potluck_local.data").joinpath("model-recipes.json")
    catalog = RecipeCatalog.model_validate_json(data.read_text(encoding="utf-8"))
    return [recipe.model_dump() for recipe in catalog.recipes]


def get_recipe(recipe_id: str) -> dict | None:
    return next((recipe for recipe in load_recipes() if recipe["id"] == recipe_id), None)


def recipe_url(recipe: dict) -> str:
    model = ModelRecipe.model_validate(recipe).model
    return f"https://huggingface.co/{model.repository}/resolve/{model.revision}/{model.filename}"


def recipe_path(recipe: dict) -> Path:
    """Keep artifacts inside their own directory; never follow a model symlink."""
    model = ModelRecipe.model_validate(recipe).model
    base = models_dir().resolve()
    if base.exists() and not base.is_dir():
        raise ValueError("The model storage path is occupied by another file")
    directory = base / model.id
    path = directory / "model.gguf"
    if directory.is_symlink():
        raise ValueError("Recipe model paths must not be symbolic links")
    if directory.exists() and not directory.is_dir():
        raise ValueError("Recipe model directory is occupied by another file")
    for name in ("model.gguf", "model.gguf.partial", "model.gguf.partial.meta", "model.gguf.sha256"):
        artifact = directory / name
        if artifact.is_symlink():
            raise ValueError("Recipe model paths must not be symbolic links")
        if artifact.exists() and not artifact.is_file():
            raise ValueError("Recipe model artifacts must be regular files")
    if directory.resolve().parent != base or path.resolve().parent != directory.resolve():
        raise ValueError("Recipe model path escapes the model directory")
    return path


def _disk_free(path: Path) -> int | None:
    # A preflight must not create ~/.potluck or a model directory just to probe it.
    try:
        existing = path
        while not existing.exists():
            if existing == existing.parent:
                return None
            existing = existing.parent
        value = shutil.disk_usage(existing).free
        return value if type(value) is int and value >= 0 else None
    except OSError:
        return None


def _available_ram() -> int | None:
    try:
        value = psutil.virtual_memory().available
        return value if type(value) is int and value >= 0 else None
    except (OSError, RuntimeError, AttributeError, ValueError):
        return None


def _remaining_download_bytes(path: Path, recipe: dict) -> int:
    """Credit only the partial the downloader can resume for this exact pin."""
    expected = recipe["model"]["size_bytes"]
    partial = path.with_name("model.gguf.partial")
    meta = path.with_name("model.gguf.partial.meta")
    try:
        if (partial.is_file() and not partial.is_symlink() and not meta.is_symlink()
                and _partial_meta_matches(meta, recipe_url(recipe), recipe["model"]["sha256"])):
            present = partial.stat().st_size
            if 0 <= present <= expected:
                return max(0, expected - present)
    except (OSError, ValueError, TypeError, AttributeError):
        # Unknown metadata never becomes disk credit or a preflight mutation.
        pass
    return expected


def _cuda_free(root: Path) -> int | None:
    """Use the same native device inventory, never a different Python backend.

    There is no recipe device-placement override, so each visible CUDA device
    must fit the complete estimate. Summing cards could promise a fit the native
    engine's default split cannot realize.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLAMA_", "GGML_RPC_"))}
    try:
        result = subprocess.run(
            [str(root / "llama-server"), "--list-devices"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, env=env, check=False,
        )
        if result.returncode or len(result.stdout) > 16384:
            return None
        devices = []
        for line in result.stdout.decode("utf-8", errors="strict").splitlines():
            if not re.match(r"\s*CUDA\d+:", line):
                continue
            match = re.fullmatch(r"\s*CUDA\d+: .+ \((\d+) MiB, (\d+) MiB free\)\s*", line)
            if not match:
                return None
            total, free = map(int, match.groups())
            if not 0 <= free <= total or total <= 0:
                return None
            devices.append(free * MIB)
        return min(devices) if 0 < len(devices) <= 16 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None


def preflight_recipe(recipe: dict, *, verify_artifact: bool = False) -> dict:
    """Check a pinned recipe without changing settings, downloading or loading."""
    parsed = ModelRecipe.model_validate(recipe)
    recipe = parsed.model_dump()
    issues: list[dict] = []

    def issue(code: str, message: str, *blocks: str) -> None:
        issues.append({"code": code, "message": message, "blocks": list(blocks)})

    weights = parsed.model.size_bytes
    kv = parsed.runtime.context_size * parsed.resources.kv_bytes_per_token
    cache = parsed.runtime.native_cache_mib * MIB
    overhead = parsed.resources.overhead_bytes
    available_ram = _available_ram()
    resources = {
        "estimated_ram_bytes": weights + kv + cache + overhead,
        "available_ram_bytes": available_ram,
        "disk_free_bytes": None,
        "required_disk_bytes": weights + DISK_MARGIN_BYTES,
    }
    warnings = [ESTIMATE_WARNING]
    if available_ram is None:
        issue("ram_unknown", "Available system memory could not be measured.", "load")
    elif available_ram < resources["estimated_ram_bytes"]:
        issue("ram_insufficient", "Not enough available RAM for the recipe's declared estimate.", "load")

    host = (platform.system(), platform.machine())
    platforms = {(p.os, p.arch) for p in parsed.engine.platforms}
    if host not in platforms:
        issue("platform_unsupported", "This recipe does not target this operating system and architecture.", "load")
    if parsed.engine.commit != PIN["commit"]:
        issue("engine_commit_mismatch", "This recipe requires a different native engine commit.", "load")
    root = None
    try:
        root, manifest = engine_bundle()
        if manifest.get("commit") != parsed.engine.commit:
            issue("engine_commit_mismatch", "The installed native engine does not match this recipe's commit.", "load")
        if parsed.engine.acceleration != "cpu" and manifest.get("backend") != parsed.engine.acceleration:
            issue("engine_acceleration_mismatch", "The installed native engine does not provide this recipe's acceleration.", "load")
    except (SplitError, OSError, ValueError):
        issue("engine_missing", "Install a Potluck build containing the pinned native engine before loading.", "load")

    if parsed.engine.acceleration == "cuda":
        vram = _cuda_free(root) if root and host in platforms else None
        resources["estimated_vram_bytes"] = weights + kv + overhead
        resources["available_vram_bytes"] = vram
        warnings.append("CUDA requires the full VRAM estimate on each visible device; device memory is not pooled by this check.")
        if vram is None:
            issue("vram_unknown", "Live free CUDA memory could not be measured with the pinned engine.", "load")
        elif vram < resources["estimated_vram_bytes"]:
            issue("vram_insufficient", "Not enough free CUDA memory for the recipe's declared estimate.", "load")

    installed = False
    verified = False
    try:
        path = recipe_path(recipe)
        installed = path.is_file()
        if installed:
            resources["required_disk_bytes"] = 0
            if path.stat().st_size != weights:
                issue("artifact_size_mismatch", "The installed file size differs from this recipe. It will not be overwritten.", "install", "load")
            elif not verify_artifact:
                issue("artifact_unverified", "Verify the installed artifact's SHA-256 before using this recipe.", "install", "load")
            else:
                digest, stamp = artifact_identity(path)
                if digest != parsed.model.sha256 or stamp[2] != weights:
                    issue("artifact_hash_mismatch", "The installed artifact differs from this recipe's SHA-256. It will not be overwritten.", "install", "load")
                else:
                    recipe_path(recipe)
                    verified = True
        else:
            issue("artifact_missing", "Install the recipe's exact artifact before loading.", "load")
            resources["required_disk_bytes"] = (
                _remaining_download_bytes(path, recipe) + DISK_MARGIN_BYTES
            )
        free = _disk_free(path.parent)
        resources["disk_free_bytes"] = free
        if free is None:
            issue("disk_unknown", "Free space in the model filesystem could not be measured.", "install")
        elif free < resources["required_disk_bytes"]:
            issue("disk_insufficient", "Not enough free space in the model filesystem for this download.", "install")
    except ValueError as exc:
        issue("artifact_path_invalid", str(exc), "install", "load")
    except OSError:
        issue("artifact_unreadable", "The installed model path could not be read safely; retry after any download completes.", "install", "load")

    return {
        "schema_version": 1, "recipe": recipe,
        "can_install": not any("install" in item["blocks"] for item in issues),
        "can_load": not any("load" in item["blocks"] for item in issues),
        "installed": installed, "artifact_verified": verified,
        "issues": issues, "warnings": warnings, "resources": resources,
    }
