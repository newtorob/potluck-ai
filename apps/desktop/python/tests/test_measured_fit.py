"""Setup options judged from what was measured here, and at most three recommended models.

The stores are real (the proof store and the benchmark store, in a temporary data
directory). The proofs are written the way the runtime writes them, for files that
exist in a temporary models directory. Only the facts about the build (backend,
driver, library) are fixed, so a result "from another driver" can be made.
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from potluck_local.services import acceptance as a
from potluck_local.services import driver_fingerprint as df
from potluck_local.services import llama_backend, model_evidence
from potluck_local.services import model_setup as setup
from potluck_local.services.model_evidence import file_identity

GIB = setup.GIB
DAY = 86400.0


class Lab:
    def __init__(self, tmp_path, monkeypatch, *, available=6.0, total=16.0) -> None:
        self.base = tmp_path / "models"
        self.base.mkdir()
        self.entries: list[dict] = []
        self.loaded: str | None = None
        self.hardware = {"ram_total_gb": total, "ram_available_gb": available, "gpu_name": None}
        self.driver, self.library, self.backend = "macOS 15.7", "siglib01", "bundled"
        monkeypatch.setattr(setup, "models_dir", lambda: self.base)
        monkeypatch.setattr(setup, "detect_hardware", lambda: self.hardware)
        monkeypatch.setattr(setup.shutil, "disk_usage", lambda _: SimpleNamespace(free=500 * GIB))
        monkeypatch.setattr(setup, "get_runtime", lambda: SimpleNamespace(
            is_loaded=lambda: self.loaded is not None, current_model_id=lambda: self.loaded))
        monkeypatch.setattr(setup.model_registry, "list_models", lambda: [dict(e) for e in self.entries])
        monkeypatch.setattr(llama_backend, "current_selection",
                            lambda: llama_backend.Selection(self.backend, None, "test", [], probed=False))
        monkeypatch.setattr(llama_backend, "library_signature", lambda: self.library)
        monkeypatch.setattr(df, "driver_fingerprint", lambda name=None: self.driver)
        monkeypatch.setattr(a, "wrapper_version", lambda: "0.3.36")
        self.monkeypatch = monkeypatch

    # -- the machine's models

    def model(self, slug, gib, *, installed=True, featured=False, min_ram=0, loaded=False) -> str:
        size = int(gib * GIB)
        entry = {"slug": slug, "size_bytes": size, "is_featured": featured, "min_ram_gb": min_ram, "installed": installed}
        if installed:
            entry["size_on_disk"] = size
            folder = self.base / slug
            folder.mkdir(exist_ok=True)
            with (folder / "model.gguf").open("wb") as stream:
                stream.truncate(size)
            old = time.time() - 3600
            os.utime(folder / "model.gguf", (old, old))
        self.entries.append(entry)
        if loaded:
            self.loaded = slug
        return slug

    def file(self, slug):
        return self.base / slug / "model.gguf"

    # -- what the machine has shown

    def key(self, slug, *, level="quick", driver=None, library=None, backend=None) -> dict:
        return a.build_key(sha256=slug.ljust(64, "0"), backend=backend or self.backend, device="Apple M2 Pro", device_kind="gpu",
                           n_gpu_layers=-1, total_layers=25, cpu_only=False, driver=driver or self.driver,
                           library=library or self.library, level=level)

    def prove(self, slug, *, verdict=a.OK, host=None, gpu=None, ctx=4096, fall=True, age_days=0.0, **key) -> None:
        """A stored proof for the file as it is now, with a measurement of `host` GiB when given."""
        record = {"verdict": verdict, "level": "quick", "duration_ms": 5, "output_tokens": 10, "checks": [],
                  "model": file_identity(self.file(slug), slug)}
        if host is not None:
            used = int(host * GIB)
            record["measurement"] = {
                "host_fall_bytes": used if fall else None, "available_before_bytes": 9 * GIB, "available_min_bytes": 9 * GIB - used,
                "rss_before_bytes": 100, "rss_peak_bytes": 100 + used // 2, "gpu_bytes": None if gpu is None else int(gpu * GIB),
                "gpu_source": None if gpu is None else "ggml_device_free", "samples": 9, "seconds": 1.0, "window": "load+checks",
                "n_ctx": ctx, "n_gpu_layers": -1, "offloaded_layers": 25, "total_layers": 25, "measured_at": "2026-10-09T10:00:00Z"}
        key_now = self.key(slug, **key)
        a.get_store().put(key_now, record)
        if age_days:  # only this proof gets older
            path = a.get_store().path()
            payload = json.loads(path.read_text())
            payload["proofs"][a.key_id(key_now)]["checked_at_epoch"] -= age_days * DAY
            path.write_text(json.dumps(payload))

    def bench(self, slug, tps, **key) -> None:
        result = {"model_id": slug, "output_tokens": 64, "ttft_ms": 50.0, "duration_ms": 900.0, "tokens_per_sec": tps,
                  "prompt_tokens_per_sec": 800.0}
        model_evidence.get_benchmark_store().put(self.key(slug, level="benchmark", **key), {
            "model": file_identity(self.file(slug), slug), "result": result, "n_ctx": 4096})

    def options(self) -> dict:
        return setup.setup_options()

    def get(self, slug) -> dict:
        return next(m for m in self.options()["models"] if m["id"] == slug)

    def picks(self) -> list[str]:
        return [p["id"] for p in self.options()["recommended"]]


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    return Lab(tmp_path, monkeypatch)


# ---- measured and unmeasured -------------------------------------------------------------


def test_a_measured_model_is_judged_from_the_measurement_and_says_so(lab):
    lab.model("a", 2)
    lab.prove("a", host=1.0, gpu=1.3)
    lab.bench("a", 42.5)
    m = lab.get("a")
    assert m["fitBasis"] == "measured" and m["fits"] is True and m["reasons"] == []
    assert m["measured"] == {"hostGiB": 1.0, "gpuGiB": 1.3, "contextTokens": 4096, "tokensPerSecond": 42.5,
                             "checkedAt": "2026-10-09T10:00:00Z"}
    assert m["checked"] == "passed"
    assert m["estimatedWorkingGiB"] == 3.9, "the estimate stays, unchanged, for every model"


def test_a_model_with_no_measurement_keeps_the_estimate_and_says_so(lab):
    lab.model("a", 2)
    m = lab.get("a")
    assert (m["fitBasis"], m["measured"], m["checked"], m["fits"]) == ("estimate", None, None, True)


def test_a_measurement_can_say_a_model_fits_where_the_estimate_refused(lab):
    lab.model("b", 4)  # estimate 6.3 GiB against 6 available
    assert lab.get("b")["fits"] is False and lab.get("b")["reasons"] == [
        "Needs about 6.3 GiB available RAM and 6.3 GiB total RAM."]
    lab.prove("b", host=3.0)  # a GPU machine: the weights are on the card, the host only paid 3 GiB
    assert lab.get("b")["fits"] is True and lab.get("b")["fitBasis"] == "measured"


def test_a_measurement_can_say_a_model_does_not_fit_where_the_estimate_allowed_it(lab):
    lab.model("c", 1)  # estimate 2.7 GiB
    assert lab.get("c")["fits"] is True
    lab.prove("c", host=5.8)
    c = lab.get("c")
    assert c["fits"] is False and c["fitBasis"] == "measured"
    assert c["reasons"] == ["Needs about 6.4 GiB free memory: 5.8 GiB measured on this machine plus a margin of 0.6 GiB. "
                            "6 GiB is free now."]


@pytest.mark.parametrize("host,available,fits", [
    (2.0, 2.4, False), (2.0, 2.5, True),    # a small model: the half-GiB floor
    (5.0, 5.4, False), (5.0, 5.5, True),    # 10% is exactly the floor
    (10.0, 10.9, False), (10.0, 11.0, True),  # a large one: 10% of it
])
def test_the_margin_is_a_tenth_of_the_measurement_and_never_under_half_a_gib(tmp_path, monkeypatch, host, available, fits):
    lab = Lab(tmp_path, monkeypatch, available=available, total=32)
    lab.model("m", 1)
    lab.prove("m", host=host)
    assert lab.get("m")["fits"] is fits


def test_the_catalogs_minimum_memory_does_not_override_a_measurement(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=6, total=8)
    lab.model("d", 2, min_ram=16)
    assert lab.get("d")["fits"] is False  # the estimate respects it
    lab.prove("d", host=1.5)
    assert lab.get("d")["fits"] is True


def test_the_total_memory_is_still_needed_to_judge_anything(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.hardware = {"gpu_name": None}
    lab.model("a", 1, installed=False)
    assert lab.get("a")["reasons"] == ["Available memory could not be measured."]


def test_on_unified_memory_the_gpu_allocation_is_part_of_what_the_host_gave_up(lab):
    lab.model("a", 2)
    lab.prove("a", host=0.7, gpu=1.3)
    assert lab.get("a")["measured"]["hostGiB"] == 0.7  # a card with its own memory: the GPU part is not the host's
    path = a.get_store().path()
    payload = json.loads(path.read_text())
    for proof in payload["proofs"].values():
        proof["measurement"]["gpu_shares_host"] = True
    path.write_text(json.dumps(payload))
    assert lab.get("a")["measured"] == {**lab.get("a")["measured"], "hostGiB": 1.3, "gpuGiB": 1.3}


def test_a_measurement_with_no_host_figure_is_not_used(lab):
    lab.model("a", 2)
    lab.prove("a", host=1.0, fall=False)  # the fall could not be read; the resident growth (0.5) still can
    assert lab.get("a")["measured"]["hostGiB"] == 0.5
    lab.model("z", 2)
    lab.prove("z", verdict=a.OK)  # a proof written without a measurement
    assert lab.get("z")["measured"] is None and lab.get("z")["checked"] == "passed" and lab.get("z")["fitBasis"] == "estimate"


def test_a_result_taken_with_another_driver_library_or_backend_says_nothing_here(lab):
    lab.model("a", 2)
    for change in ({"driver": "macOS 14.0"}, {"library": "siglib02"}, {"backend": "vulkan"}):
        lab.prove("a", host=1.0, **change)
    m = lab.get("a")
    assert (m["measured"], m["checked"], m["fitBasis"]) == (None, None, "estimate")


def test_a_result_for_a_file_that_has_since_been_replaced_says_nothing(lab):
    lab.model("a", 2)
    lab.prove("a", host=1.0)
    lab.bench("a", 50.0)
    assert lab.get("a")["checked"] == "passed"
    old = time.time() - 60
    os.utime(lab.file("a"), (old, old))  # a different modification time: not the file that was measured
    m = lab.get("a")
    assert (m["measured"], m["checked"]) == (None, None)
    lab.model("b", 2)
    lab.prove("b", host=1.0)
    with lab.file("b").open("ab") as stream:
        stream.write(b"x")  # a different size
    assert lab.get("b")["measured"] is None


def test_a_measurement_is_for_installed_files_only(lab):
    lab.model("a", 2)
    lab.prove("a", host=1.0)
    lab.entries[0]["installed"] = False
    lab.entries[0].pop("size_on_disk")
    assert lab.get("a")["measured"] is None


def test_the_newest_usable_proof_decides_and_an_old_one_ages_out(lab):
    lab.model("a", 2)
    lab.prove("a", host=1.0, level="quick", age_days=2)
    lab.prove("a", host=1.4, level="full")
    assert lab.get("a")["measured"]["hostGiB"] == 1.4
    lab.prove("a", host=1.0, level="quick", age_days=a.MAX_AGE_DAYS + 1)
    assert lab.get("a")["measured"]["hostGiB"] == 1.4


def test_a_failed_check_is_reported_and_a_week_old_failure_is_forgotten(lab):
    lab.model("a", 2)
    lab.prove("a", verdict=a.MODEL_FAILED, host=1.0)
    assert lab.get("a")["checked"] == "failed"
    lab.prove("a", verdict=a.MODEL_FAILED, host=1.0, age_days=a.MODEL_FAILED_DAYS + 1)
    assert lab.get("a")["checked"] is None


def test_a_gpu_that_gave_wrong_answers_is_not_a_verdict_on_the_model(lab):
    lab.model("a", 2)
    lab.prove("a", verdict=a.GPU_WRONG_OUTPUT, host=1.0)
    assert (lab.get("a")["checked"], lab.get("a")["measured"]) == (None, None)
    lab.prove("a", verdict=a.OK, host=2.0, driver="the cpu path")  # another key, which does not match this driver
    assert lab.get("a")["checked"] is None


def test_no_proofs_at_all_never_touches_the_backend(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch)
    lab.model("a", 2)

    def boom():
        raise AssertionError("asked the backend with nothing to match")

    monkeypatch.setattr(llama_backend, "current_selection", boom)
    assert lab.get("a")["fitBasis"] == "estimate"


# ---- the loaded model goes away first ---------------------------------------------------


def test_a_loaded_models_measured_need_is_added_back_when_judging_another(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=1.0)
    lab.model("big", 5, loaded=True)
    lab.model("other", 3)
    lab.prove("big", host=5.0)
    lab.prove("other", host=4.0)  # needs 4.5 with its margin; 1 free now, 6 once big is gone
    other = lab.get("other")
    assert other["fits"] is True and other["reasons"] == []
    lab.loaded = None
    assert lab.get("other")["fits"] is False, "with nothing loaded the same model does not fit"


def test_a_loaded_model_with_no_measurement_gives_back_the_model_sized_part_of_its_estimate(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=0.5)
    lab.model("big", 4, loaded=True)  # 4 * 1.2 = 4.8 GiB comes back, not the 1.5 GiB reserve as well
    lab.model("fits", 3.5)  # estimate 5.7: needs 5.3 free after, so exactly not enough
    lab.model("also", 2.5)  # estimate 4.5 <= 0.5 + 4.8
    assert lab.get("fits")["fits"] is False
    assert lab.get("also")["fits"] is True


def test_the_arithmetic_is_available_plus_what_comes_back_against_the_need(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=2.0)
    lab.model("big", 5, loaded=True)
    lab.prove("big", host=3.0)
    lab.model("x", 2)
    lab.prove("x", host=4.1)  # 4.1 + 0.5 = 4.6 needed, 2.0 + 3.0 = 5.0 free after
    assert lab.get("x")["fits"] is True
    lab.model("y", 2)
    lab.prove("y", host=4.6)  # 4.6 + 0.5 = 5.1 needed
    assert lab.get("y")["fits"] is False


def test_the_reason_says_which_figure_came_back(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=1.0)
    lab.model("big", 5, loaded=True)
    lab.prove("big", host=3.0)
    lab.model("huge", 9)
    lab.prove("huge", host=9.0)
    (reason,) = lab.get("huge")["reasons"]
    assert reason == ("Needs about 9.9 GiB free memory: 9 GiB measured on this machine plus a margin of 0.9 GiB. "
                      "1 GiB is free now. About 4 GiB would be free once big is unloaded (measured 3 GiB).")
    lab.model("est", 12)
    (reason,) = lab.get("est")["reasons"]
    assert reason == ("Needs about 15.9 GiB available RAM and 15.9 GiB total RAM. "
                      "About 4 GiB would be free once big is unloaded (measured 3 GiB).")


def test_an_estimated_give_back_is_named_as_an_estimate(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=1.0)
    lab.model("big", 5, loaded=True)
    lab.model("est", 12)
    (reason,) = lab.get("est")["reasons"]
    assert reason.endswith("About 7 GiB would be free once big is unloaded (estimated 6 GiB).")


def test_the_loaded_model_itself_never_needs_its_memory_twice(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=0.1)
    lab.model("big", 5, loaded=True)
    assert lab.get("big")["fits"] is True
    lab.prove("big", host=5.0)
    assert lab.get("big")["fits"] is True and lab.get("big")["fitBasis"] == "measured"


def test_a_loaded_model_that_is_not_in_the_list_gives_back_nothing(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=1.0)
    lab.model("x", 3)
    lab.loaded = "some-hf-model"  # a pipeline or whole-model load has no entry here
    assert lab.get("x")["fits"] is False
    assert lab.get("x")["reasons"] == ["Needs about 5.1 GiB available RAM and 5.1 GiB total RAM."]


def test_with_nothing_loaded_the_estimate_text_is_what_it_always_was(lab):
    lab.model("b", 4)
    assert lab.get("b")["reasons"] == ["Needs about 6.3 GiB available RAM and 6.3 GiB total RAM."]


# ---- at most three recommended models ----------------------------------------------------


def proven(lab, slug, gib, tps, **kw):
    lab.model(slug, gib, **kw)
    lab.prove(slug, host=gib * 0.3)
    if tps is not None:
        lab.bench(slug, tps)
    return slug


def test_proven_and_fast_models_come_first_the_biggest_first(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    proven(lab, "small", 1, 90.0)
    proven(lab, "big", 4, 20.0)
    proven(lab, "mid", 2, 45.0)
    assert lab.picks() == ["big", "mid", "small"]
    assert [p["basis"] for p in lab.options()["recommended"]] == ["proven_fast"] * 3
    assert lab.options()["recommended"][0] == {"id": "big", "basis": "proven_fast", "tokensPerSecond": 20.0}


def test_fifteen_tokens_a_second_is_fast_and_a_little_under_is_not(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    proven(lab, "exact", 2, 15.0)
    proven(lab, "under", 3, 14.9)
    assert lab.picks() == ["exact", "under"]
    assert [p["basis"] for p in lab.options()["recommended"]] == ["proven_fast", "proven_slow"]


def test_equal_size_ties_go_to_the_faster_model_then_to_the_name(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    proven(lab, "b", 2, 30.0)
    proven(lab, "a", 2, 30.0)
    proven(lab, "c", 2, 50.0)
    assert lab.picks() == ["c", "a", "b"]


def test_models_that_passed_but_are_slow_follow_the_fast_ones_fastest_first(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=40)
    proven(lab, "fast-small", 1, 80.0)
    proven(lab, "slow-a", 8, 9.0)
    proven(lab, "slow-b", 6, 12.0)
    got = lab.options()["recommended"]
    assert [p["id"] for p in got] == ["fast-small", "slow-b", "slow-a"]
    assert [p["basis"] for p in got] == ["proven_fast", "proven_slow", "proven_slow"]


def test_a_model_that_passed_with_no_speed_comes_after_the_ones_with_a_speed_smaller_first(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=40)
    proven(lab, "no-speed-big", 5, None)
    proven(lab, "no-speed-small", 2, None)
    proven(lab, "slow", 6, 12.0)
    got = lab.options()["recommended"]
    assert [(p["id"], p["basis"], p["tokensPerSecond"]) for p in got] == [
        ("slow", "proven_slow", 12.0), ("no-speed-small", "proven_speed_unknown", None),
        ("no-speed-big", "proven_speed_unknown", None)]


def test_proven_models_come_before_unproven_ones(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    lab.model("unproven-big", 6, featured=True)
    proven(lab, "proven-small", 1, 70.0)
    assert lab.picks() == ["proven-small", "unproven-big"]
    assert [p["basis"] for p in lab.options()["recommended"]] == ["proven_fast", "estimate"]


def test_unproven_models_that_fit_are_ordered_running_then_featured_then_here_then_smaller(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=30)
    lab.model("plain-small", 1, installed=False)
    lab.model("featured-big", 6, installed=False, featured=True)
    lab.model("featured-small", 2, installed=False, featured=True)
    lab.model("here", 3, installed=True)
    lab.model("running", 4, installed=True, loaded=True)
    assert lab.picks() == ["running", "featured-small", "featured-big"]
    lab.loaded = None
    assert lab.picks() == ["featured-small", "featured-big", "here"]
    lab.model("featured-here", 5, installed=True, featured=True)  # featured and already on disk: no download to wait for
    assert lab.picks() == ["featured-here", "featured-small", "featured-big"]


def test_a_model_whose_check_failed_here_is_never_recommended(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    proven(lab, "good", 1, 50.0)
    lab.model("bad", 5, loaded=True, featured=True)
    lab.prove("bad", verdict=a.MODEL_FAILED, host=1.0)
    lab.bench("bad", 80.0)
    out = lab.options()
    assert [p["id"] for p in out["recommended"]] == ["good"] and "bad" in [m["id"] for m in out["models"]]
    assert out["recommendedModel"] == "good", "the older single field skips a model that failed too, though it is loaded"


def test_a_model_that_does_not_fit_is_never_recommended_however_well_it_did(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=2)
    proven(lab, "huge", 20, 60.0)  # measured 6 GiB, and 2 are free
    proven(lab, "ok", 1, 30.0)
    assert lab.get("huge")["fits"] is False
    assert lab.picks() == ["ok"]


def test_fewer_than_three_candidates_give_a_shorter_list_and_none_gives_an_empty_one(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    assert lab.options()["recommended"] == [] and lab.options()["recommendedModel"] is None
    proven(lab, "one", 1, 30.0)
    assert lab.picks() == ["one"]


def test_more_than_three_are_cut_to_the_three_strongest(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=60)
    for i, size in enumerate((1, 2, 3, 4, 5)):
        proven(lab, f"m{i}", size, 40.0)
    lab.model("unproven", 1, installed=False, featured=True)
    assert lab.picks() == ["m4", "m3", "m2"]


def test_each_recommendation_has_exactly_an_id_a_basis_and_a_speed(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=12)
    proven(lab, "a", 1, 30.0)
    lab.model("b", 1, installed=False)
    assert all(set(p) == {"id", "basis", "tokensPerSecond"} for p in lab.options()["recommended"])
    assert len({p["id"] for p in lab.options()["recommended"]}) == len(lab.options()["recommended"])


def test_recommended_model_keeps_its_old_meaning_whatever_the_proofs_say(tmp_path, monkeypatch):
    lab = Lab(tmp_path, monkeypatch, available=30)
    proven(lab, "fast-big", 6, 90.0)
    lab.model("loaded-small", 1, loaded=True)
    lab.model("installed-featured", 2, featured=True)
    assert lab.picks()[0] == "fast-big"
    assert lab.options()["recommendedModel"] == "loaded-small", "loaded first, as before"
    lab.loaded = None
    assert lab.options()["recommendedModel"] == "installed-featured", "then installed, then featured, then the smallest"



# ---- additive only ----------------------------------------------------------------------

OLD_TOP = {"schemaVersion": int, "hardware": dict, "diskFreeBytes": int, "currentModel": (str, type(None)),
           "recommendedModel": (str, type(None)), "models": list}
OLD_MODEL = {"id": str, "name": str, "sizeBytes": int, "installed": bool, "loaded": bool, "fits": bool, "reasons": list,
             "estimatedWorkingGiB": float, "featured": bool}
OLD_HARDWARE = {"os", "arch", "cpu_name", "cpu_cores_physical", "ram_total_gb", "ram_available_gb", "gpu_name"}


@pytest.mark.parametrize("with_evidence", [False, True])
def test_every_old_key_is_still_there_with_its_old_type_and_the_new_ones_are_added(tmp_path, monkeypatch, with_evidence):
    lab = Lab(tmp_path, monkeypatch, available=12)
    lab.model("a", 2, featured=True)
    lab.model("b", 3, installed=False)
    if with_evidence:
        lab.prove("a", host=1.0, gpu=1.2)
        lab.bench("a", 33.0)
    out = lab.options()
    assert out["schemaVersion"] == 1
    for key, kind in OLD_TOP.items():
        assert isinstance(out[key], kind), key
    assert set(out["hardware"]) == OLD_HARDWARE | {"gpu_vram_gb", "inference_gpu_available", "inference_gpu_vram_gb"}
    for model in out["models"]:
        for key, kind in OLD_MODEL.items():
            assert isinstance(model[key], kind) and not (kind is int and isinstance(model[key], bool)), key
    assert set(out) == set(OLD_TOP) | {"recommended", "useCase"}
    assert all(set(m) == set(OLD_MODEL) | {"fitBasis", "measured", "checked", "recommendationReason", "useCases"} for m in out["models"])
    assert json.loads(json.dumps(out)) == out, "everything in it is plain JSON"


def test_the_old_values_do_not_move_for_a_machine_with_no_evidence(tmp_path, monkeypatch):
    """The same inputs as the first test of the older suite: same answer, same words."""
    lab = Lab(tmp_path, monkeypatch, available=6)
    lab.model("large", 8, featured=True, min_ram=24)
    lab.model("small", 1, min_ram=4, installed=False)
    lab.model("medium", 2, featured=True, min_ram=8, installed=False)
    out = lab.options()
    by = {m["id"]: m for m in out["models"]}
    assert out["recommendedModel"] == "medium"
    assert by["large"]["fits"] is False and by["large"]["reasons"] == ["Needs about 11.1 GiB available RAM and 24 GiB total RAM."]
    assert (by["small"]["estimatedWorkingGiB"], by["medium"]["estimatedWorkingGiB"]) == (2.7, 3.9)
    assert all(m["fitBasis"] == "estimate" and m["measured"] is None and m["checked"] is None for m in out["models"])
