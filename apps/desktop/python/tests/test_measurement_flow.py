"""What a load costs, stored with the proof; a benchmark that outlives a restart.

The runtime is real, including load_model. Only the native Llama is replaced by a
fake that "takes" memory from a fake machine when it is constructed (and, for the
checks, when it first answers), and the fake machine is what the sampler reads.
Nothing here says anything about a real GPU or real memory; the real-model figures
are in the task report.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from potluck_local.services import acceptance as a
from potluck_local.services import driver_fingerprint as df
from potluck_local.services import llama_backend, memory_probe, model_evidence
from potluck_local.services.runtime import LLMRuntime
from test_acceptance_checks import FakeModel

GIB = 1024 ** 3
SLUG = "qwen-test"
MAC = [{"name": "MTL0", "description": "Apple M2 Pro", "type": "gpu", "free_bytes": 12 * GIB, "total_bytes": 16 * GIB},
       {"name": "CPU", "description": "Apple M2 Pro", "type": "cpu", "free_bytes": 0, "total_bytes": 0}]


class Machine:
    """The fake machine the sampler reads and the fake Llama draws on."""

    def __init__(self) -> None:
        self.available = 10 * GIB
        self.rss = 200 * 1024 ** 2
        self.gpu_free = 12 * GIB
        self.load_cost = 3 * GIB       # host memory a load takes
        self.load_gpu = 2 * GIB        # the part of it on the GPU, for a load with layers offloaded
        self.check_cost = GIB // 2     # more taken the first time the model answers
        self.cpu_load_cost = 4 * GIB   # a CPU-only load keeps the weights in host memory
        self.mode = {"gpu": "good", "cpu": "good"}
        self.loads = 0

    def device(self) -> list[dict]:
        return [{**MAC[0], "free_bytes": self.gpu_free}, dict(MAC[1])]


class FakeLlama(FakeModel):
    machine: Machine

    def __init__(self, model_path, n_ctx=512, n_gpu_layers=0, n_threads=None, verbose=True, **kw) -> None:
        m = self.machine
        self.on_gpu = n_gpu_layers != 0
        super().__init__(m.mode["gpu" if self.on_gpu else "cpu"])
        cost = m.load_cost if self.on_gpu else m.cpu_load_cost
        self.held = {"available": cost, "rss": cost // 2, "gpu_free": m.load_gpu if self.on_gpu else 0}
        self._take(self.held)
        m.loads += 1
        self._answered = False
        time.sleep(0.02)  # a few ticks of the sampler

    def _take(self, held: dict) -> None:
        m = self.machine
        m.available -= held["available"]
        m.rss += held["rss"]
        m.gpu_free -= held["gpu_free"]

    def create_chat_completion(self, **kw):
        if not self._answered:
            self._answered = True
            more = {"available": self.machine.check_cost, "rss": 0,
                    "gpu_free": self.machine.check_cost // 2 if self.on_gpu else 0}
            self._take(more)
            self.held = {k: self.held[k] + more[k] for k in more}
        return super().create_chat_completion(**kw)

    def close(self) -> None:
        """Unloading gives everything this model took back to the machine."""
        m = self.machine
        m.available += self.held["available"]
        m.rss -= self.held["rss"]
        m.gpu_free += self.held["gpu_free"]
        self.held = {"available": 0, "rss": 0, "gpu_free": 0}


class Rig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        import llama_cpp

        self.monkeypatch = monkeypatch
        self.machine = Machine()
        self.rt = LLMRuntime()
        self.driver: str | None = "macOS 15.7 (24G222)"
        self.library = "siglib01"
        self.backend = "bundled"
        FakeLlama.machine = self.machine
        monkeypatch.setattr(llama_cpp, "Llama", FakeLlama)
        monkeypatch.setattr(llama_backend, "current_selection",
                            lambda: llama_backend.Selection(self.backend, None, "test", [], probed=False))
        monkeypatch.setattr(llama_backend, "describe_devices", lambda *args: self.machine.device())
        monkeypatch.setattr(llama_backend, "library_signature", lambda: self.library)
        monkeypatch.setattr(llama_backend, "read_gguf_layer_count", lambda path: 25)
        monkeypatch.setattr(df, "driver_fingerprint", lambda name=None: self.driver)
        monkeypatch.setattr(a, "wrapper_version", lambda: "0.3.36")
        monkeypatch.setattr(memory_probe, "read_available", lambda: self.machine.available)
        monkeypatch.setattr(memory_probe, "read_rss", lambda: self.machine.rss)
        monkeypatch.setattr(memory_probe, "INTERVAL_SECONDS", 0.002)
        monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
        self.dir = tmp_path / "models"
        monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
        self.path = self.dir / SLUG / "model.gguf"
        self.path.parent.mkdir(parents=True)
        self.set_weights(b"weights v1")

    def set_weights(self, data: bytes) -> None:
        self.path.write_bytes(data)
        old = time.time() - 3600
        os.utime(self.path, (old, old))

    def load(self, *, verify=True, n_ctx=4096, rpc_servers=None, runtime=None, settle=True) -> dict:
        """The load, and then the recorded checks it left to run (settle=False stops at the verdict)."""
        rt = runtime or self.rt
        result = rt._load_with_check(str(self.path), verify=verify, n_ctx=n_ctx, n_gpu_layers=-1,
                                     n_threads=None, rpc_servers=rpc_servers)
        if settle:
            async def run() -> None:
                rt._launch_recorded()
                await rt.wait_for_recorded()

            asyncio.run(run())
        return result

    def proofs(self) -> dict:
        path = a.get_store().path()
        return json.loads(path.read_text())["proofs"] if path.exists() else {}

    def proof(self, **match) -> dict:
        (found,) = [p for p in self.proofs().values() if all(p["key"].get(k) == v for k, v in match.items())]
        return found


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def sampler_threads():
    return [t for t in threading.enumerate() if t.name == "memory-sampler" and t.is_alive()]


# ---- the cost of a load is stored with the proof ---------------------------------------


def test_a_load_and_its_check_store_what_they_cost_with_the_proof(rig):
    rig.load()
    proof = rig.proof()
    m = proof["measurement"]
    assert proof["verdict"] == "ok"
    # 3 GiB for the load and half a GiB the first answer needed: the lowest point, from the start
    assert m["host_fall_bytes"] == 3 * GIB + GIB // 2
    assert m["available_before_bytes"] == 10 * GIB and m["available_min_bytes"] == 10 * GIB - (3 * GIB + GIB // 2)
    assert m["rss_peak_bytes"] - m["rss_before_bytes"] == 3 * GIB // 2  # the fake process grows by half of the load
    assert m["gpu_bytes"] == 2 * GIB + GIB // 4 and m["gpu_source"] == "ggml_device_free"
    assert m["n_ctx"] == 4096 and m["n_gpu_layers"] == -1 and m["window"] == "load+checks"
    assert m["measured_at"].endswith("Z") and m["samples"] >= 3
    assert sampler_threads() == []


def test_the_proof_says_which_file_the_measurement_is_for(rig):
    rig.load()
    stat = os.stat(rig.path)
    assert rig.proof()["model"] == {"id": SLUG, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def test_what_the_load_took_is_seen_even_when_the_check_adds_nothing(rig):
    rig.machine.check_cost = 0
    rig.load()
    assert rig.proof()["measurement"]["host_fall_bytes"] == 3 * GIB


def test_a_reused_proof_keeps_the_measurement_it_was_stored_with(rig):
    rig.load()
    first = rig.proof()["measurement"]
    rig.rt.unload_model()
    rig.machine.available, rig.machine.load_cost = 10 * GIB, 1 * GIB  # this load takes far less
    rig.load()
    assert rig.rt.acceptance_summary()["from_proof"] is True and rig.machine.loads == 2
    assert rig.proof()["measurement"] == first


def test_a_proof_stored_before_measurements_existed_gets_the_loads_reading_once(rig):
    rig.load()
    (key_id,) = rig.proofs()
    stored = rig.proofs()[key_id]
    old = {k: v for k, v in stored.items() if k not in ("measurement", "model")}  # what an older engine wrote
    path = a.get_store().path()
    path.write_text(json.dumps({"schema_version": 1, "proofs": {key_id: old}}))
    rig.rt.unload_model()
    rig.machine.available = 10 * GIB
    rig.load()
    proof = rig.proof()
    assert proof["measurement"]["window"] == "load" and proof["measurement"]["host_fall_bytes"] == 3 * GIB
    assert proof["model"]["id"] == SLUG
    assert proof["checked_at_epoch"] == old["checked_at_epoch"], "the time of the check does not move"
    kept = proof["measurement"]
    rig.rt.unload_model()
    rig.machine.available, rig.machine.load_cost = 10 * GIB, 6 * GIB
    rig.load()
    assert rig.proof()["measurement"] == kept, "attached once, never replaced by a load that ran no checks"


def test_a_forced_check_extends_the_loaded_models_window_and_stores_the_new_reading(rig):
    rig.machine.check_cost = 0
    rig.load()
    assert rig.proof()["measurement"]["host_fall_bytes"] == 3 * GIB
    rig.machine.available -= 1 * GIB  # the checks need 1 GiB more than they did
    rig.rt._accept_loaded(SLUG, None, "quick", True)
    m = rig.proof()["measurement"]
    assert m["host_fall_bytes"] == 4 * GIB and m["window"] == "load+checks"
    assert m["available_before_bytes"] == 10 * GIB, "the baseline stays the load's, not the moment of the re-check"


def test_a_model_that_failed_its_check_still_has_its_cost_stored(rig):
    rig.machine.mode = {"gpu": "wrong", "cpu": "wrong"}
    rig.load()
    proof = rig.proof(device_kind="gpu")
    assert proof["verdict"] == "model_failed" and proof["measurement"]["host_fall_bytes"] >= 3 * GIB


def test_a_gpu_that_gave_wrong_answers_and_the_cpu_that_did_not_each_keep_their_own_reading(rig):
    rig.machine.mode = {"gpu": "doubled", "cpu": "good"}
    rig.load()
    gpu, cpu = rig.proof(device_kind="gpu"), rig.proof(device_kind="cpu")
    assert (gpu["verdict"], cpu["verdict"]) == ("gpu_wrong_output", "ok")
    assert gpu["measurement"]["gpu_bytes"] is not None and gpu["measurement"]["n_gpu_layers"] == -1
    # The CPU reload starts from what was free after the first load was released, and has no GPU figure.
    assert cpu["measurement"]["n_gpu_layers"] == 0 and cpu["measurement"]["gpu_bytes"] is None
    assert cpu["measurement"]["host_fall_bytes"] >= 4 * GIB
    assert sampler_threads() == []


def test_a_load_with_the_check_off_stores_nothing(rig):
    rig.load(verify=False)
    assert rig.proofs() == {} and sampler_threads() == []


def test_a_load_over_rpc_servers_is_not_measured(rig):
    rig.rt.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1, rpc_servers=["100.64.0.6:50052"])
    assert rig.rt._load_memory is None and sampler_threads() == []


def test_the_sampler_is_stopped_when_the_native_load_fails(rig, monkeypatch):
    import llama_cpp

    class Boom:
        def __init__(self, *a, **k):
            time.sleep(0.01)
            raise MemoryError("not enough memory")

    monkeypatch.setattr(llama_cpp, "Llama", Boom)
    with pytest.raises(MemoryError):
        rig.rt.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1)
    assert sampler_threads() == [] and rig.rt._load_memory is None


def test_the_sampler_is_stopped_when_the_checks_raise(rig, monkeypatch):
    def boom(*args, **kw):
        raise RuntimeError("check crashed")

    monkeypatch.setattr(a, "run_checks", boom)
    rig.load()  # a fault in the check must not undo a good load
    assert rig.rt.is_loaded() and sampler_threads() == []


def test_the_window_starts_after_the_previous_model_is_gone(rig):
    """Loading over a loaded model must not count the freed model as memory the new one saved."""
    rig.rt.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1)
    assert rig.machine.available == 7 * GIB
    rig.machine.load_cost = 2 * GIB
    rig.rt.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1)  # frees 3 GiB, then takes 2
    m = rig.rt._load_memory
    assert m["available_before_bytes"] == 10 * GIB and m["host_fall_bytes"] == 2 * GIB


def test_a_cpu_only_load_has_no_gpu_figure(rig):
    rig.rt.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1, cpu_only=True)
    assert rig.rt._load_memory["gpu_bytes"] is None and rig.rt._load_memory["host_fall_bytes"] == 4 * GIB


def test_unloading_forgets_the_window(rig):
    rig.load()
    assert rig.rt._load_memory is not None
    rig.rt.unload_model()
    assert rig.rt._load_memory is None and rig.rt._gpu_meter is None


def test_the_measurement_does_not_change_what_the_summary_reports(rig):
    rig.load()
    s = rig.rt.acceptance_summary()
    assert set(s) == {"schema_version", "status", "model_id", "level", "checked_at", "from_proof", "ran_on", "checks",
                      "verdict", "fallback", "message", "recorded"}


# ---- benchmarks outlive a restart --------------------------------------------------------


class StreamingLlama(FakeLlama):
    def create_chat_completion(self, *, stream=False, **kw):
        if not stream:
            return super().create_chat_completion(stream=stream, **kw)
        return iter([{"choices": [{"delta": {"content": "tok"}}]} for _ in range(8)])

    def tokenize(self, data, add_bos=True, special=False):
        return list(range(700))


@pytest.fixture()
def benched(rig, monkeypatch):
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "Llama", StreamingLlama)
    rig.load()
    result = asyncio.run(rig.rt.benchmark())
    return rig, result


def test_a_benchmark_is_still_there_after_a_restart_and_keeps_its_shape(benched):
    rig, result = benched
    assert set(result) == {"model_id", "output_tokens", "ttft_ms", "duration_ms", "tokens_per_sec", "prompt_tokens_per_sec"}
    restarted = LLMRuntime()
    assert restarted.benchmarks() == {SLUG: result}
    assert restarted.is_loaded() is False


def test_the_persisted_record_says_when_and_for_which_setup(benched):
    rig, result = benched
    (entry,) = model_evidence.get_benchmark_store().entries()
    assert entry["result"] == result and entry["checked_at"].endswith("Z")
    assert entry["key"]["level"] == "benchmark" and entry["key"]["n_gpu_layers"] == -1 and entry["key"]["driver"] == rig.driver
    assert entry["model"]["id"] == SLUG


def test_the_status_and_the_inference_report_show_the_last_benchmark_after_a_restart(benched, monkeypatch):
    from potluck_local.routers import health
    from potluck_local.services import performance_status

    rig, result = benched
    restarted = LLMRuntime()
    restarted.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1)  # the engine loads the same model again at start
    monkeypatch.setattr(performance_status, "get_runtime", lambda: restarted)
    monkeypatch.setattr(health, "get_runtime", lambda: restarted)
    status = asyncio.run(performance_status.performance_status())
    assert status["local"]["benchmarkTokensPerSecond"] == result["tokens_per_sec"]
    assert status["local"]["benchmarkTtftMs"] == result["ttft_ms"]
    report = asyncio.run(health.inference_diagnostics())
    assert report["benchmark"] == result


def test_a_benchmark_of_this_process_wins_over_the_remembered_one(benched):
    rig, result = benched
    restarted = LLMRuntime()
    fresh = {**result, "tokens_per_sec": 999.0}
    restarted._benchmarks[SLUG] = fresh
    assert restarted.benchmarks()[SLUG] == fresh


@pytest.mark.parametrize("change", [
    lambda r: setattr(r, "driver", "macOS 16.0"),
    lambda r: setattr(r, "library", "siglib02"),
    lambda r: setattr(r, "backend", "vulkan"),
    lambda r: r.set_weights(b"weights v2, different"),
], ids=["new driver", "new library", "other backend", "new weights"])
def test_a_benchmark_from_another_setup_is_not_shown(benched, change):
    rig, _ = benched
    change(rig)
    assert LLMRuntime().benchmarks() == {}


def test_a_benchmark_taken_on_the_gpu_is_not_shown_for_the_same_model_now_running_on_the_cpu(benched):
    rig, result = benched
    cpu = LLMRuntime()
    cpu.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1, cpu_only=True)
    assert cpu.benchmarks() == {}
    gpu = LLMRuntime()
    gpu.load_model(str(rig.path), n_ctx=4096, n_gpu_layers=-1)
    assert gpu.benchmarks() == {SLUG: result}


def test_a_benchmark_is_kept_even_when_the_answer_check_is_off(rig, monkeypatch):
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "Llama", StreamingLlama)
    rig.load(verify=False)
    result = asyncio.run(rig.rt.benchmark())
    assert rig.proofs() == {} and LLMRuntime().benchmarks() == {SLUG: result}


def test_an_old_benchmark_ages_out_like_a_proof(benched):
    rig, _ = benched
    store = model_evidence.get_benchmark_store()
    payload = json.loads(store.path().read_text())
    for entry in payload["benchmarks"].values():
        entry["checked_at_epoch"] -= (a.MAX_AGE_DAYS + 1) * 86400
    store.path().write_text(json.dumps(payload))
    assert LLMRuntime().benchmarks() == {}


def test_a_damaged_benchmark_file_reads_as_none_and_never_breaks_the_status(benched):
    rig, _ = benched
    model_evidence.get_benchmark_store().path().write_text("{not json")
    assert LLMRuntime().benchmarks() == {}


def test_the_remembered_results_are_read_once_in_a_while_not_on_every_ask(benched, monkeypatch):
    rig, result = benched
    restarted = LLMRuntime()
    reads = []
    real = model_evidence.persisted_benchmarks
    monkeypatch.setattr(model_evidence, "persisted_benchmarks", lambda *a, **k: reads.append(1) or real(*a, **k))
    for _ in range(5):
        assert restarted.benchmarks() == {SLUG: result}
    assert len(reads) == 1
