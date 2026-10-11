"""What this machine has already shown about a model.

Three things are kept from earlier loads and read back here, for the setup
options (model_setup.py) and for the benchmark the engine remembers across a
restart (runtime.benchmarks):

  the answer check   a stored proof (acceptance.py): passed or failed
  what it cost       the measurement stored with that proof (memory_probe.py)
  how fast it ran    the last benchmark result, stored under the same kind of key

A stored result counts only when it describes what would load now: the same file
(name, size and modification time, so no multi-gigabyte hash is needed to ask),
and a key that matches this backend, llama-cpp-python version, backend library,
check suite and graphics driver. A result from another driver or library is not
evidence about this one, so it is ignored and the model counts as not measured.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from potluck_local.services import acceptance

logger = logging.getLogger(__name__)

# Verdicts whose load worked. A GPU that gave wrong answers describes a load that
# will not be made again (the CPU proof beside it is the one that counts).
_USABLE_VERDICTS = (acceptance.OK, acceptance.MODEL_FAILED)


def file_identity(path: str | Path, model_id: str | None) -> dict | None:
    """Which file a result was taken on: model id, size and modification time."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {"id": model_id, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def benchmark_path() -> Path:
    from potluck_local.utils.paths import data_dir

    return data_dir() / "data" / "benchmark-results.json"


_benchmark_store = acceptance.ProofStore(benchmark_path, section="benchmarks")


def get_benchmark_store() -> acceptance.ProofStore:
    return _benchmark_store


class Environment:
    """The build this engine would load a model with right now."""

    def __init__(self) -> None:
        from potluck_local.services import llama_backend

        self.backend = llama_backend.current_selection().backend
        self.llama_cpp = acceptance.wrapper_version()
        self.library = llama_backend.library_signature()
        self._drivers: dict[str | None, str | None] = {}

    def _driver(self, key: dict) -> str | None:
        from potluck_local.services.driver_fingerprint import driver_fingerprint

        name = None if key.get("device_kind") == "cpu" else key.get("device")
        if name not in self._drivers:
            self._drivers[name] = driver_fingerprint(name)
        return self._drivers[name]

    def accepts(self, key: dict) -> bool:
        return (
            key.get("backend") == self.backend
            and key.get("llama_cpp") == self.llama_cpp
            and key.get("library") == self.library
            and key.get("suite") == acceptance.SUITE_VERSION
            and key.get("driver") == self._driver(key)
        )


def environment() -> Environment | None:
    try:
        return Environment()
    except Exception as exc:  # noqa: BLE001 - no environment means no evidence, never an error
        logger.debug("model_evidence: environment unreadable (%s)", type(exc).__name__)
        return None


@dataclass(frozen=True)
class Evidence:
    """checked: "passed" or "failed" (None when there is no usable proof for this file).
    measurement: the stored measurement dict. benchmark: the stored benchmark result."""

    checked: str | None = None
    measurement: dict | None = None
    benchmark: dict | None = None


def _by_model(entries: list[dict], env: Environment) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:  # newest first
        model = entry.get("model")
        if isinstance(model, dict) and isinstance(model.get("id"), str) and env.accepts(entry["key"]):
            grouped.setdefault(model["id"], []).append(entry)
    return grouped


def gather(files: dict[str, Path]) -> dict[str, Evidence]:
    """Evidence for each installed model, by id. Models with none are left out."""
    try:
        proofs = acceptance.get_store().entries()
        benches = get_benchmark_store().entries()
        if not proofs and not benches:
            return {}
        env = environment()
        if env is None:
            return {}
        proofs_by_model, benches_by_model = _by_model(proofs, env), _by_model(benches, env)
        found: dict[str, Evidence] = {}
        for slug, path in files.items():
            identity = file_identity(path, slug)
            mine = [p for p in proofs_by_model.get(slug, []) if p["model"] == identity and p.get("verdict") in _USABLE_VERDICTS]
            bench = next((b for b in benches_by_model.get(slug, []) if b["model"] == identity), None)
            checked = {acceptance.OK: "passed", acceptance.MODEL_FAILED: "failed"}.get(mine[0]["verdict"]) if mine else None
            measurement = next((p["measurement"] for p in mine if isinstance(p.get("measurement"), dict)), None)
            if checked or measurement or bench:
                found[slug] = Evidence(checked, measurement, bench.get("result") if bench else None)
        return found
    except Exception as exc:  # noqa: BLE001 - setup options must still answer
        logger.debug("model_evidence: gather failed (%s)", type(exc).__name__)
        return {}


def persisted_benchmarks(models_dir: Path, loaded: dict | None = None) -> dict[str, dict]:
    """The remembered benchmark result for each model in `models_dir`, in the shape runtime.benchmarks() returns.

    `loaded` (the loaded model's id and the layer split it runs with) keeps a result taken with a
    different split, say on the GPU, from being shown for a model now running on the CPU."""
    try:
        benches = get_benchmark_store().entries()
        if not benches:
            return {}
        env = environment()
        if env is None:
            return {}
        out: dict[str, dict] = {}
        for slug, entries in _by_model(benches, env).items():
            identity = file_identity(models_dir / slug / "model.gguf", slug)
            for entry in entries:  # newest first
                key = entry["key"]
                if entry["model"] != identity or not isinstance(entry.get("result"), dict):
                    continue
                if loaded and loaded.get("id") == slug and (
                    key.get("cpu_only") != loaded.get("cpu_only") or key.get("n_gpu_layers") != loaded.get("n_gpu_layers")
                ):
                    continue
                out[slug] = entry["result"]
                break
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_evidence: persisted benchmarks unreadable (%s)", type(exc).__name__)
        return {}
