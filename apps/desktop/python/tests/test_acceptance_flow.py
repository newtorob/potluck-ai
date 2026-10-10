"""The answer check inside the model load: proofs, the CPU cross-check and the
states the engine can be left in.

The runtime is real. Only the native load is replaced: a fake load decides
where the model "runs" (GPU or CPU) and hands back a fake model that answers
well or badly depending on that. Nothing here says anything about a real GPU.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from potluck_local.services import acceptance as a
from potluck_local.services import driver_fingerprint as df
from potluck_local.services import llama_backend
from potluck_local.services.model_identity import artifact_identity
from potluck_local.services.runtime import LLMRuntime
from test_acceptance_checks import FakeModel

DAY = 86400.0
GIB = 1024 ** 3
SLUG = "qwen-test"


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, gpu="good", cpu="good", backend="vulkan") -> None:
        self.monkeypatch = monkeypatch
        self.rt = LLMRuntime()
        self.gpu, self.cpu = gpu, cpu  # how the model answers on each device
        self.backend = backend
        self.device_name = "AMD Radeon RX 6600 XT"
        self.driver: str | None = "31.0.21025.10 (2-11-2025)"
        self.library = "siglib01"
        self.loads: list[tuple[int, bool]] = []  # (layers used, cpu_only), one per load that happened
        self.models: list[FakeModel] = []
        self.refuse_cpu_load: BaseException | None = None
        self.fail_after: int | None = None  # every load past this many raises
        self.path = tmp_path / "models" / SLUG / "model.gguf"
        self.path.parent.mkdir(parents=True)
        self.set_weights(b"weights v1")
        monkeypatch.setattr(self.rt, "load_model", self._load)
        monkeypatch.setattr(llama_backend, "current_selection",
                            lambda: llama_backend.Selection(self.backend, None, "test", [], probed=False))
        monkeypatch.setattr(llama_backend, "describe_devices", lambda *args: [self.device()])
        monkeypatch.setattr(llama_backend, "library_signature", lambda: self.library)
        monkeypatch.setattr(llama_backend, "read_gguf_layer_count", lambda path: 32)
        monkeypatch.setattr(df, "driver_fingerprint", lambda name=None: self.driver)
        monkeypatch.setattr(a, "wrapper_version", lambda: "0.3.36")

    def device(self) -> dict:
        return {"name": "Vulkan0", "description": self.device_name, "type": "gpu",
                "free_bytes": 7 * GIB, "total_bytes": 8 * GIB}

    def set_weights(self, data: bytes) -> None:
        self.path.write_bytes(data)
        old = time.time() - 3600
        os.utime(self.path, (old, old))

    def _load(self, path, *, n_ctx, n_gpu_layers, n_threads, rpc_servers, cpu_only=False) -> dict:
        rt = self.rt
        rt._release_model()  # the real load frees the previous model first
        if self.fail_after is not None and len(self.loads) >= self.fail_after:
            raise MemoryError("not enough memory")
        if cpu_only and self.refuse_cpu_load is not None:
            raise self.refuse_cpu_load
        used = 0 if (cpu_only or self.backend == "cpu") else n_gpu_layers
        self.loads.append((used, cpu_only))
        model = FakeModel(self.gpu if used else self.cpu)
        self.models.append(model)
        gpu = used != 0
        rt._model, rt._model_path, rt._model_id = model, str(path), Path(path).parent.name
        rt._artifact = rt._verified_artifact = artifact_identity(Path(path))
        rt._loaded_n_ctx = n_ctx
        rt._load_report = {
            "backend_selection": {"backend": self.backend, "devices": [self.device()]},
            "requested_n_gpu_layers": n_gpu_layers, "used_n_gpu_layers": used, "cpu_only": cpu_only,
            "offload_device": self.device() if gpu else None,
            "offloaded_layers": 33 if gpu else 0, "total_layers": 33,
            "model_buffer_mib": {"Vulkan0": 100.0} if gpu else {"CPU_Mapped": 100.0},
        }
        return {"model_id": rt._model_id, "n_ctx": n_ctx, "n_gpu_layers": used, "inference": rt.inference_summary()}

    def load(self, *, verify=True, n_gpu_layers=-1, rpc_servers=None) -> dict:
        return self.rt._load_with_check(str(self.path), verify=verify, n_ctx=4096, n_gpu_layers=n_gpu_layers,
                                        n_threads=None, rpc_servers=rpc_servers)

    def check(self, level="quick", force=False):
        return self.rt._accept_loaded(SLUG, None, level, force)

    def summary(self) -> dict:
        return self.rt.acceptance_summary()

    def checks_ran_on_last_load(self) -> bool:
        return bool(self.models[-1].asked)

    def proofs(self) -> dict:
        path = a.get_store().path()
        return json.loads(path.read_text())["proofs"] if path.exists() else {}


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def make(tmp_path, monkeypatch, **kw) -> Rig:
    return Rig(tmp_path, monkeypatch, **kw)


# ---- ways a setup can change, and a proof can age


def _new_weights(rig):
    rig.set_weights(b"weights v2, a different file")


def _other_backend(rig):
    rig.backend = "cuda"


def _other_device(rig):
    rig.device_name = "NVIDIA GeForce RTX 2080 Ti"


def _other_driver(rig):
    rig.driver = "560.35.03"


def _unknown_driver(rig):
    rig.driver = None


def _other_library(rig):
    rig.library = "siglib02"


def _other_suite(rig):
    rig.monkeypatch.setattr(a, "SUITE_VERSION", a.SUITE_VERSION + 1)


def _other_wrapper(rig):
    rig.monkeypatch.setattr(a, "wrapper_version", lambda: "0.3.99")


def _age(days: float) -> None:
    store = a.get_store()
    payload = json.loads(store.path().read_text())
    for proof in payload["proofs"].values():
        proof["checked_at_epoch"] -= days * DAY
    store.path().write_text(json.dumps(payload))


# ---- a model that answers well ---------------------------------------------------


def test_a_good_model_on_the_gpu_passes_and_the_result_says_where_it_ran(rig):
    result = rig.load()
    s = rig.summary()
    assert (s["status"], s["verdict"], s["from_proof"], s["fallback"], s["message"]) == ("passed", "ok", False, None, None)
    assert s["model_id"] == SLUG and s["level"] == "quick" and s["schema_version"] == 1
    assert s["ran_on"] == {"device_kind": "gpu", "backend": "vulkan", "device_name": "AMD Radeon RX 6600 XT",
                           "driver": "31.0.21025.10 (2-11-2025)"}
    assert {c["id"] for c in s["checks"] if c["required"]} == {"copy", "arithmetic", "repeats"}
    assert all(c["passed"] for c in s["checks"]) and s["checked_at"].endswith("Z")
    assert rig.rt.answers_verified() is True
    assert rig.loads == [(-1, False)] and result["n_gpu_layers"] == -1


def test_a_passing_proof_is_reused_and_nothing_runs(rig):
    rig.load()
    first = rig.summary()
    rig.rt.unload_model()
    rig.load()
    s = rig.summary()
    assert s["status"] == "passed" and s["from_proof"] is True and s["verdict"] == "ok"
    assert rig.models[-1].asked == [] and rig.loads == [(-1, False), (-1, False)]
    assert s["checks"] == first["checks"] and s["checked_at"] == first["checked_at"]
    assert s["ran_on"] == first["ran_on"]


def test_the_proof_is_kept_with_short_excerpts_for_the_owner(rig):
    rig.load()
    (proof,) = rig.proofs().values()
    assert proof["verdict"] == "ok" and proof["key"]["model_sha256"] == rig.rt.current_model_sha256()
    assert proof["key"]["device"] == "AMD Radeon RX 6600 XT" and proof["key"]["driver"] == rig.driver
    assert all(len(c["excerpt"]) <= 80 for c in proof["checks"])


def test_nothing_is_served_between_the_load_and_its_check(rig):
    """The check runs inside the lifecycle step that holds both gates."""
    held = []
    real = a.run_checks

    def spy(model, level, **kw):
        held.append((rig.rt._inference_lock.locked(), rig.rt._admission_lock.locked(), rig.summary()["status"]))
        return real(model, level, **kw)

    rig.monkeypatch.setattr(a, "run_checks", spy)
    asyncio.run(rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True))
    assert held == [(True, True, "running")]
    assert rig.summary()["status"] == "passed"


def test_leaving_the_check_alone_changes_nothing(rig):
    asyncio.run(rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1))
    assert rig.models[-1].asked == [] and rig.summary()["status"] == "not_run"
    assert rig.rt.answers_verified() is False


# ---- a broken GPU ------------------------------------------------------------------


def test_wrong_gpu_and_right_cpu_stays_on_the_cpu_with_the_contract_words(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    result = rig.load()
    s = rig.summary()
    assert rig.loads == [(-1, False), (0, True)]
    assert (s["status"], s["verdict"]) == ("passed", "gpu_wrong_output")
    assert s["fallback"] == {"from": "gpu", "to": "cpu", "reason": "gpu_wrong_output"}
    assert s["message"] == ("The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. "
                            "Updating the graphics driver usually fixes this.")
    assert s["ran_on"]["device_kind"] == "cpu" and s["ran_on"]["device_name"] == "CPU"
    assert result["n_gpu_layers"] == 0 and result["inference"]["device_kind"] == "cpu"
    assert rig.rt.answers_verified() is True  # right answers on the CPU may be lent
    verdicts = {p["key"]["device_kind"]: p["verdict"] for p in rig.proofs().values()}
    assert verdicts == {"gpu": "gpu_wrong_output", "cpu": "ok"}


def test_the_cpu_cross_check_loads_with_no_gpu_help_at_all(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    assert rig.rt._load_report["cpu_only"] is True and rig.rt._load_report["used_n_gpu_layers"] == 0


def test_the_next_load_goes_straight_to_the_cpu(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.rt.unload_model()
    rig.loads.clear()
    rig.load()
    s = rig.summary()
    assert rig.loads == [(0, True)], "the known-bad GPU is not loaded again"
    assert rig.models[-1].asked == [] and s["from_proof"] is True
    assert (s["status"], s["verdict"]) == ("passed", "gpu_wrong_output") and s["fallback"] is not None
    assert s["message"] == a.GPU_WRONG_TEXT


def test_a_new_driver_tries_the_gpu_again(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.rt.unload_model()
    rig.driver, rig.gpu = "32.0.12011.1009 (9-3-2025)", "good"  # the new driver fixes it
    rig.loads.clear()
    rig.load()
    s = rig.summary()
    assert rig.loads == [(-1, False)]
    assert (s["status"], s["verdict"], s["fallback"], s["from_proof"]) == ("passed", "ok", None, False)
    assert s["ran_on"]["device_kind"] == "gpu" and s["ran_on"]["driver"] == "32.0.12011.1009 (9-3-2025)"


def test_a_new_driver_that_is_still_wrong_falls_back_again_and_remembers_it(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.rt.unload_model()
    rig.driver = "32.0.12011.1009 (9-3-2025)"
    rig.loads.clear()
    rig.load()
    assert rig.loads == [(-1, False), (0, True)]
    assert sum(1 for p in rig.proofs().values() if p["verdict"] == "gpu_wrong_output") == 2


def test_a_forced_check_on_a_cpu_fallback_asks_the_gpu_again(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.gpu = "good"
    rig.loads.clear()
    rig.check(force=True)
    s = rig.summary()
    assert rig.loads == [(-1, False)] and rig.rt._load_report["used_n_gpu_layers"] == -1
    assert (s["status"], s["verdict"], s["fallback"]) == ("passed", "ok", None)
    assert rig.rt._load_args is not None


def test_a_gpu_load_found_bad_by_a_stored_proof_is_moved_to_the_cpu(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.rt.unload_model()
    rig.load(verify=False)  # loaded while the check was off: on the GPU
    assert rig.rt._load_report["used_n_gpu_layers"] == -1 and rig.summary()["status"] == "skipped"
    rig.loads.clear()
    rig.check()
    assert rig.loads == [(0, True)] and rig.summary()["fallback"] is not None
    assert rig.summary()["from_proof"] is True


# ---- a weak model --------------------------------------------------------------------


def test_wrong_on_both_is_the_model_and_the_planned_gpu_load_is_put_back(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    result = rig.load()
    s = rig.summary()
    assert rig.loads == [(-1, False), (0, True), (-1, False)]
    assert (s["status"], s["verdict"], s["fallback"]) == ("failed", "model_failed", None)
    assert s["message"] == ("This model did not pass its answer check on this machine. You can still use it yourself. "
                            "It is not offered to other people.")
    assert s["ran_on"]["device_kind"] == "gpu" and result["n_gpu_layers"] == -1
    assert [c["passed"] for c in s["checks"] if c["required"]] == [False, False, False]
    assert rig.rt.answers_verified() is False and rig.rt.is_loaded()
    assert "gpu_wrong_output" not in {p["verdict"] for p in rig.proofs().values()}


def test_a_model_that_failed_is_remembered_and_the_next_load_is_not_repeated(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    rig.rt.unload_model()
    rig.loads.clear()
    result = rig.load()
    s = rig.summary()
    assert rig.loads == [(-1, False)], "one load, on the planned GPU, and it stays"
    assert rig.models[-1].asked == []
    assert (s["status"], s["verdict"], s["from_proof"], s["fallback"]) == ("failed", "model_failed", True, None)
    assert s["message"] == a.MODEL_FAILED_TEXT and s["ran_on"]["device_kind"] == "gpu"
    assert [c["passed"] for c in s["checks"] if c["required"]] == [False, False, False]
    assert result["n_gpu_layers"] == -1 and rig.rt.answers_verified() is False


@pytest.mark.parametrize("days,reused", [(6, True), (8, False)])
def test_a_failed_proof_is_believed_for_seven_days(tmp_path, monkeypatch, days, reused):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    rig.rt.unload_model()
    _age(days)
    rig.loads.clear()
    rig.load()
    assert (rig.loads == [(-1, False)]) is reused
    assert rig.summary()["from_proof"] is reused


def test_the_seven_days_apply_when_the_driver_is_unknown_too(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.driver = None
    rig.load()
    rig.rt.unload_model()
    _age(6)
    rig.load()
    assert rig.summary()["from_proof"] is True
    rig.rt.unload_model()
    _age(2)
    rig.load()
    assert rig.summary()["from_proof"] is False


def test_force_runs_everything_again_for_a_model_that_failed(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    rig.loads.clear()
    summary, _ = rig.check(force=True)
    assert rig.loads == [(0, True), (-1, False)]
    assert summary["from_proof"] is False and summary["verdict"] == "model_failed"


def test_a_model_that_failed_and_is_fixed_passes_once_forced(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    rig.models[-1].mode = rig.gpu = "good"  # something outside the key was fixed
    rig.loads.clear()
    summary, _ = rig.check(force=True)
    assert (summary["status"], summary["verdict"], summary["from_proof"]) == ("passed", "ok", False)
    rig.rt.unload_model()
    rig.load()
    assert rig.summary()["status"] == "passed" and rig.summary()["from_proof"] is True


@pytest.mark.parametrize("change", [_new_weights, _other_backend, _other_device, _other_driver, _unknown_driver,
                                    _other_library, _other_suite, _other_wrapper],
                         ids=lambda f: f.__name__.removeprefix("_"))
def test_each_change_to_the_setup_runs_a_failed_model_again(tmp_path, monkeypatch, change):
    rig = make(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    rig.rt.unload_model()
    change(rig)
    rig.loads.clear()
    rig.load()
    assert rig.loads == [(-1, False), (0, True), (-1, False)]
    assert rig.summary()["from_proof"] is False


def test_a_failure_on_a_cpu_only_load_is_the_model_and_nothing_is_cross_checked(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, backend="cpu", cpu="wrong")
    rig.load()
    s = rig.summary()
    assert rig.loads == [(0, False)]
    assert (s["status"], s["verdict"], s["fallback"]) == ("failed", "model_failed", None)
    assert s["ran_on"]["device_kind"] == "cpu"


def test_a_pinned_zero_layers_is_a_cpu_load_too(rig):
    rig.gpu = rig.cpu = "wrong"
    rig.load(n_gpu_layers=0)
    assert rig.loads == [(0, False)] and rig.summary()["verdict"] == "model_failed"


def test_a_failure_on_a_cpu_only_load_is_remembered_too(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, backend="cpu", cpu="wrong")
    rig.load()
    rig.rt.unload_model()
    rig.loads.clear()
    rig.load()
    assert rig.loads == [(0, False)] and rig.models[-1].asked == []
    assert (rig.summary()["status"], rig.summary()["from_proof"]) == ("failed", True)


def test_a_stored_gpu_proof_that_the_cpu_now_contradicts_is_dropped(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    rig.rt.unload_model()
    store = a.get_store()
    for proof in rig.proofs().values():  # forget the CPU's pass but keep the GPU's fail
        if proof["verdict"] == "ok":
            store.drop(proof["key"])
    rig.cpu = "wrong"
    rig.loads.clear()
    rig.load()
    assert rig.loads == [(0, True), (-1, False)]
    assert rig.summary()["verdict"] == "model_failed"
    assert not store.holds_gpu_wrong_output()
    # What replaced it is remembered, so the next start does not do this again.
    rig.rt.unload_model()
    rig.loads.clear()
    rig.load()
    assert rig.loads == [(-1, False)] and rig.summary()["from_proof"] is True


# ---- a load that fails while the CPU is being tried --------------------------------------


def test_running_out_of_memory_on_the_cpu_attempt_puts_the_gpu_load_back_and_says_so(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.refuse_cpu_load = MemoryError("host memory")
    rig.load()
    s = rig.summary()
    assert rig.loads == [(-1, False), (-1, False)]
    assert rig.rt.is_loaded() and rig.rt._load_report["used_n_gpu_layers"] == -1
    assert (s["status"], s["verdict"], s["fallback"], s["model_id"]) == ("failed", "model_failed", None, SLUG)
    assert s["ran_on"]["device_kind"] == "gpu" and s["message"] == a.MODEL_FAILED_TEXT
    assert rig.rt.answers_verified() is False
    assert rig.rt.inference_summary()["device_kind"] == "gpu"
    assert rig.proofs() == {}, "nothing was learned about the GPU, so nothing is remembered"


def test_when_even_the_gpu_load_cannot_be_put_back_nothing_is_loaded_and_the_state_says_so(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.refuse_cpu_load = MemoryError("host memory")
    rig.fail_after = 1
    with pytest.raises(MemoryError):
        rig.load()
    assert not rig.rt.is_loaded() and rig.rt.current_model_id() is None
    s = rig.summary()
    assert (s["status"], s["model_id"], s["ran_on"], s["checks"]) == ("not_run", None, None, [])
    assert rig.rt.answers_verified() is None and rig.rt.inference_summary() is None


# ---- the setting and the force flag -----------------------------------------------------------


def test_with_the_check_turned_off_nothing_runs_and_nothing_moves(tmp_path, monkeypatch):
    rig = make(tmp_path, monkeypatch, gpu="doubled")
    rig.load(verify=False)
    s = rig.summary()
    assert rig.loads == [(-1, False)] and rig.models[-1].asked == []
    assert (s["status"], s["verdict"], s["fallback"], s["message"], s["checks"]) == ("skipped", None, None, None, [])
    assert s["model_id"] == SLUG
    assert rig.rt.answers_verified() is False
    assert not a.get_store().path().exists()


def test_force_ignores_the_stored_proof_and_runs_now(rig):
    rig.load()
    rig.rt.unload_model()
    rig.load()
    assert rig.models[-1].asked == []
    summary, tokens = rig.check(force=True)
    assert rig.models[-1].asked, "the checks ran"
    assert summary["from_proof"] is False and summary["status"] == "passed" and tokens and tokens > 0


def test_without_force_a_check_reuses_the_proof(rig):
    rig.load()
    asked = len(rig.models[-1].asked)
    summary, _ = rig.check()
    assert summary["from_proof"] is True and len(rig.models[-1].asked) == asked


def test_a_forced_check_that_fails_on_a_good_machine_updates_the_state(rig):
    rig.load()
    rig.models[-1].mode = rig.cpu = "wrong"  # the loaded copy goes bad, and so would a CPU copy
    summary, _ = rig.check(force=True)
    assert summary["status"] == "failed" and summary["verdict"] == "model_failed"


def test_full_is_its_own_proof(rig):
    rig.load()
    summary, _ = rig.check("full")
    assert summary["level"] == "full" and summary["from_proof"] is False
    summary, _ = rig.check("full")
    assert summary["from_proof"] is True
    summary, _ = rig.check("quick")
    assert summary["level"] == "quick" and summary["from_proof"] is True


def test_checking_a_different_model_than_the_loaded_one_is_refused(rig):
    from potluck_local.services.model_identity import ModelIdentityError

    rig.load()
    with pytest.raises(ModelIdentityError):
        rig.rt._accept_loaded("another-model", None, "quick", False)


# ---- when a proof stops applying -------------------------------------------------------------------


@pytest.mark.parametrize("change", [_new_weights, _other_backend, _other_device, _other_driver, _unknown_driver,
                                    _other_library, _other_suite, _other_wrapper],
                         ids=lambda f: f.__name__.removeprefix("_"))
def test_each_change_to_the_setup_runs_the_checks_again(rig, change):
    rig.load()
    rig.rt.unload_model()
    change(rig)
    rig.load()
    assert rig.checks_ran_on_last_load() and rig.summary()["from_proof"] is False


def test_a_different_split_of_the_layers_runs_the_checks_again(rig):
    rig.load()
    rig.rt.unload_model()
    rig.load(n_gpu_layers=20)
    assert rig.checks_ran_on_last_load()


def test_an_unchanged_setup_does_not(rig):
    rig.load()
    rig.rt.unload_model()
    rig.load()
    assert not rig.checks_ran_on_last_load()


@pytest.mark.parametrize("driver,days,reused", [
    ("31.0.1", 89, True), ("31.0.1", 91, False), (None, 29, True), (None, 31, False),
])
def test_old_proofs_are_checked_again(rig, driver, days, reused):
    rig.driver = driver
    rig.load()
    rig.rt.unload_model()
    _age(days)
    rig.load()
    assert rig.checks_ran_on_last_load() is (not reused)


# ---- faults and other kinds of load ---------------------------------------------------------------------


def test_a_fault_in_the_check_does_not_undo_a_good_load(rig, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("a bug")

    monkeypatch.setattr(a, "run_checks", broken)
    result = rig.load()
    assert rig.rt.is_loaded() and result["n_gpu_layers"] == -1
    assert rig.summary()["status"] == "not_run" and rig.rt.answers_verified() is False


# ---- loads the check does not cover -----------------------------------------------------------
#
# A managed split, a pipeline or whole-model load and a load with rpc_servers are not
# checked. They stay "not_run", answers_verified() is None, and they lend as they did.


def _rpc(rig):
    rig.load(rpc_servers=["100.64.0.6:50052"])
    rig.rt._loaded_rpc_servers = ("100.64.0.6:50052",)  # what the native load records


def _pipeline(rig):
    rig.load()
    rig.rt._release_model()
    rig.rt._pipeline, rig.rt._model_id = SimpleNamespace(describe=lambda: {}), "Qwen/Qwen2.5-32B-Instruct"


def _managed_split(rig):
    rig.load()
    rig.rt._model = SimpleNamespace(managed_split=True, closed=False)


@pytest.mark.parametrize("load", [_rpc, _pipeline, _managed_split], ids=lambda f: f.__name__.removeprefix("_"))
def test_loads_the_check_does_not_cover_are_not_run_and_answers_verified_is_null(rig, load):
    load(rig)
    assert rig.rt.is_loaded() and rig.rt.answers_verified() is None
    s = rig.summary()
    assert (s["status"], s["verdict"], s["fallback"], s["message"], s["checks"]) == ("not_run", None, None, None, [])
    assert s["model_id"] == rig.rt.current_model_id()
    assert a.lending_allowed(rig.rt) is True


@pytest.mark.parametrize("load", [_rpc, _pipeline, _managed_split], ids=lambda f: f.__name__.removeprefix("_"))
def test_the_setting_does_not_change_what_the_check_never_covered(rig, load):
    from potluck_local.services.settings_manager import get_settings, save_settings

    load(rig)
    settings = get_settings().model_copy(deep=True)
    settings.runtime.verify_answers = False
    save_settings(settings)
    assert rig.rt.answers_verified() is None and rig.summary()["status"] == "not_run"
    assert a.lending_allowed(rig.rt) is True


def test_the_rpc_load_asks_the_model_nothing(rig):
    rig.load(rpc_servers=["100.64.0.6:50052"])
    assert rig.models[-1].asked == []


def test_a_single_machine_load_without_a_passing_check_is_never_lent(tmp_path, monkeypatch):
    skipped = make(tmp_path / "a", monkeypatch)
    skipped.load(verify=False)
    assert (skipped.rt.answers_verified(), a.lending_allowed(skipped.rt)) == (False, False)
    failed = make(tmp_path / "b", monkeypatch, gpu="wrong", cpu="wrong")
    failed.load()
    assert (failed.rt.answers_verified(), a.lending_allowed(failed.rt)) == (False, False)
    running = make(tmp_path / "c", monkeypatch)
    running.load()
    running.rt._acceptance = a.summary(status="running", model_id=SLUG)
    assert (running.rt.answers_verified(), a.lending_allowed(running.rt)) == (False, False)
    unchecked = make(tmp_path / "d", monkeypatch)
    asyncio.run(unchecked.rt.load_model_async(str(unchecked.path), n_ctx=4096, n_gpu_layers=-1))
    assert (unchecked.rt.answers_verified(), a.lending_allowed(unchecked.rt)) == (False, False)


def test_unloading_clears_the_result(rig):
    rig.load()
    rig.rt.unload_model()
    s = rig.summary()
    assert (s["status"], s["model_id"], s["checks"]) == ("not_run", None, [])
    assert rig.rt.answers_verified() is None


def test_answers_are_verified_only_when_the_status_is_passed(rig):
    rt = rig.rt
    assert rt.answers_verified() is None
    rig.load()
    for status, expected in (("passed", True), ("failed", False), ("running", False), ("not_run", False), ("skipped", False)):
        rt._acceptance = a.summary(status=status, model_id=SLUG)
        assert rt.answers_verified() is expected
    rt._acceptance = None
    assert rt.answers_verified() is False


def test_the_contract_object_has_exactly_these_keys(rig):
    rig.load()
    assert list(rig.summary()) == ["schema_version", "status", "model_id", "level", "checked_at", "from_proof",
                                   "ran_on", "checks", "verdict", "fallback", "message"]
    assert list(rig.summary()["ran_on"]) == ["device_kind", "backend", "device_name", "driver"]
    assert list(rig.summary()["checks"][0]) == ["id", "passed", "required"]
    assert list(a.summary()) == list(rig.summary())


def test_nothing_the_model_said_reaches_the_result_or_the_log(rig, monkeypatch, caplog):
    secret = "PRIVATE-MODEL-OUTPUT-7731"
    original = FakeModel.good_answer
    monkeypatch.setattr(FakeModel, "good_answer", lambda self, p: (
        f"{secret} is what the ocean told the harbor town today near the old clock tower" if "two short" in p
        else original(self, p)))
    with caplog.at_level("DEBUG"):
        rig.load()
    assert rig.summary()["status"] == "passed"
    assert secret not in json.dumps(rig.summary()) and secret not in caplog.text
    (proof,) = rig.proofs().values()  # the owner's own file may keep a short excerpt
    assert any(secret in c["excerpt"] for c in proof["checks"])


def test_turning_the_setting_off_takes_effect_at_once_and_back_on_restores_the_result(rig):
    from potluck_local.services.settings_manager import get_settings, save_settings

    def set_verify(value: bool) -> None:
        settings = get_settings().model_copy(deep=True)
        settings.runtime.verify_answers = value
        save_settings(settings)

    rig.load()
    assert rig.rt.answers_verified() is True
    set_verify(False)
    assert rig.rt.answers_verified() is False
    assert (rig.summary()["status"], rig.summary()["model_id"]) == ("skipped", SLUG)
    set_verify(True)
    assert rig.rt.answers_verified() is True and rig.summary()["status"] == "passed"
