"""The proof store: what a result is tied to, how long it is believed, and how
the file is kept."""
from __future__ import annotations

import json
import os

import pytest

from potluck_local.services import acceptance as a

DAY = 86400.0


def make_key(**changes) -> dict:
    base = dict(sha256="a" * 64, backend="vulkan", device="AMD Radeon RX 6600 XT", device_kind="gpu",
                n_gpu_layers=-1, total_layers=36, cpu_only=False, driver="31.0.21025.10 (2-11-2025)",
                library="siglib01", level="quick")
    base.update(changes)
    return a.build_key(**base)


RECORD = {"verdict": a.OK, "level": "quick", "duration_ms": 500, "output_tokens": 40,
          "checks": [{"id": "copy", "passed": True, "required": True, "excerpt": "58213"}]}


@pytest.fixture()
def store(tmp_path):
    return a.ProofStore(tmp_path / "data" / "acceptance-proofs.json")


def test_a_proof_is_found_again_for_the_same_key(store):
    store.put(make_key(), RECORD)
    proof = store.get(make_key())
    assert proof["verdict"] == "ok" and proof["checks"][0]["excerpt"] == "58213"
    assert proof["key"] == make_key() and proof["checked_at"].endswith("Z")


@pytest.mark.parametrize("change", [
    {"sha256": "b" * 64},                  # different weights
    {"backend": "cuda"},                   # different backend
    {"device": "NVIDIA GeForce RTX 2080 Ti"},
    {"device_kind": "mixed", "n_gpu_layers": 20},
    {"n_gpu_layers": 30},                  # a different split of the layers
    {"total_layers": 40},
    {"cpu_only": True},
    {"driver": "31.0.21025.11 (3-1-2025)"},   # a new driver
    {"driver": None},
    {"library": "siglib02"},               # a rebuilt backend, or another driver in the signature
    {"level": "full"},
])
def test_a_proof_does_not_apply_when_any_part_of_the_key_changes(store, change):
    store.put(make_key(), RECORD)
    assert store.get(make_key(**change)) is None
    assert store.get(make_key()) is not None


def test_a_new_check_suite_version_makes_old_proofs_stale(store, monkeypatch):
    store.put(make_key(), RECORD)
    monkeypatch.setattr(a, "SUITE_VERSION", a.SUITE_VERSION + 1)
    assert store.get(make_key()) is None


def test_a_new_wrapper_version_makes_old_proofs_stale(store, monkeypatch):
    store.put(make_key(), RECORD)
    monkeypatch.setattr(a, "wrapper_version", lambda: "9.9.9")
    assert store.get(make_key()) is None


def test_proofs_expire_after_ninety_days(store):
    store.put(make_key(), RECORD)
    stamp = store.get(make_key())["checked_at_epoch"]
    assert store.get(make_key(), now=stamp + 89 * DAY) is not None
    assert store.get(make_key(), now=stamp + 91 * DAY) is None


def test_proofs_with_an_unknown_driver_expire_after_thirty_days(store):
    key = make_key(driver=None)
    store.put(key, RECORD)
    stamp = store.get(key)["checked_at_epoch"]
    assert store.get(key, now=stamp + 29 * DAY) is not None
    assert store.get(key, now=stamp + 31 * DAY) is None


def test_the_store_is_bounded_and_keeps_the_newest(store):
    for i in range(a.MAX_PROOFS + 20):
        store.put(make_key(sha256=f"{i:064x}"), RECORD)
    proofs = json.loads(store.path().read_text())["proofs"]
    assert len(proofs) == a.MAX_PROOFS
    assert store.get(make_key(sha256=f"{a.MAX_PROOFS + 19:064x}")) is not None
    assert store.get(make_key(sha256=f"{0:064x}")) is None
    assert store.path().stat().st_size < 200_000


def test_expired_proofs_are_dropped_when_the_file_is_written(store):
    store.put(make_key(sha256="c" * 64), RECORD)
    payload = json.loads(store.path().read_text())
    for proof in payload["proofs"].values():
        proof["checked_at_epoch"] -= 100 * DAY
    store.path().write_text(json.dumps(payload))
    store.put(make_key(), RECORD)
    assert len(json.loads(store.path().read_text())["proofs"]) == 1


def test_the_file_is_replaced_atomically_and_no_temporary_file_is_left(store, monkeypatch):
    store.put(make_key(), RECORD)
    before = store.path().read_text()
    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    store.put(make_key(sha256="d" * 64), RECORD)  # a failed write is logged and ignored
    monkeypatch.setattr(os, "replace", real_replace)
    assert store.path().read_text() == before
    assert [p.name for p in store.path().parent.iterdir()] == ["acceptance-proofs.json"]


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"proofs": []}', '{"proofs": {"x": 5}}', "\x00\x01"])
def test_a_damaged_file_reads_as_empty_and_is_rebuilt(store, content):
    store.path().parent.mkdir(parents=True)
    store.path().write_text(content)
    assert store.get(make_key()) is None and store.holds_gpu_wrong_output() is False
    store.put(make_key(), RECORD)
    assert store.get(make_key()) is not None


def test_an_oversized_file_is_ignored(store):
    store.put(make_key(), RECORD)
    with open(store.path(), "a") as f:
        f.write(" " * (2 * 1024 * 1024))
    assert store.get(make_key()) is None


def test_a_missing_directory_is_created(tmp_path):
    s = a.ProofStore(tmp_path / "deep" / "er" / "p.json")
    s.put(make_key(), RECORD)
    assert s.get(make_key()) is not None


def test_a_hand_edited_key_does_not_match(store):
    store.put(make_key(), RECORD)
    payload = json.loads(store.path().read_text())
    for proof in payload["proofs"].values():
        proof["key"]["model_sha256"] = "e" * 64  # the entry no longer says what its id claims
    store.path().write_text(json.dumps(payload))
    assert store.get(make_key()) is None


def test_drop_removes_one_proof(store):
    store.put(make_key(), RECORD)
    store.put(make_key(sha256="b" * 64), RECORD)
    store.drop(make_key())
    assert store.get(make_key()) is None and store.get(make_key(sha256="b" * 64)) is not None


def test_a_machine_with_no_gpu_proof_is_told_so_cheaply(store):
    assert store.holds_gpu_wrong_output() is False
    store.put(make_key(), RECORD)
    assert store.holds_gpu_wrong_output() is False
    store.put(make_key(sha256="b" * 64), {**RECORD, "verdict": a.GPU_WRONG_OUTPUT})
    assert store.holds_gpu_wrong_output() is True


def test_the_default_location_is_in_the_engine_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    s = a.ProofStore()
    s.put(make_key(), RECORD)
    assert (tmp_path / "data" / "acceptance-proofs.json").is_file()
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path / "other"))
    assert s.get(make_key()) is None  # the location follows the data directory


# ---- what a load cost, kept with the proof; the same file format holds benchmarks -----------

MEASURED = {"measurement": {"host_fall_bytes": 123, "n_ctx": 4096}, "model": {"id": "m", "size": 1, "mtime_ns": 2}}


def test_a_newer_result_for_the_same_key_keeps_the_cost_and_the_file_when_it_brings_none(store):
    store.put(make_key(), {**RECORD, **MEASURED})
    store.put(make_key(), RECORD)  # a later check that measured nothing
    proof = store.get(make_key())
    assert proof["measurement"] == MEASURED["measurement"] and proof["model"] == MEASURED["model"]


def test_a_newer_measurement_replaces_the_older_one(store):
    store.put(make_key(), {**RECORD, **MEASURED})
    store.put(make_key(), {**RECORD, "measurement": {"host_fall_bytes": 456}})
    assert store.get(make_key())["measurement"] == {"host_fall_bytes": 456}


def test_a_cost_is_never_carried_to_a_different_key(store):
    store.put(make_key(), {**RECORD, **MEASURED})
    store.put(make_key(driver="32.0.1"), RECORD)
    assert "measurement" not in store.get(make_key(driver="32.0.1"))


def test_attach_adds_only_what_is_missing_and_leaves_the_time_of_the_check_alone(store):
    store.put(make_key(), RECORD)
    before = store.get(make_key())
    store.attach(make_key(), {"measurement": {"host_fall_bytes": 1}, "verdict": "model_failed", "model": {"id": "m"}})
    after = store.get(make_key())
    assert after["measurement"] == {"host_fall_bytes": 1} and after["model"] == {"id": "m"}
    assert after["verdict"] == "ok", "a stored field is never replaced"
    assert after["checked_at_epoch"] == before["checked_at_epoch"]
    store.attach(make_key(), {"measurement": {"host_fall_bytes": 2}})
    assert store.get(make_key())["measurement"] == {"host_fall_bytes": 1}


def test_attach_to_a_key_that_is_not_stored_does_nothing(store):
    store.attach(make_key(), {"measurement": {}})
    assert not store.path().exists()


def test_entries_lists_what_has_not_aged_out_newest_first(store):
    store.put(make_key(sha256="a" * 64), RECORD)
    store.put(make_key(sha256="b" * 64), RECORD)
    store.put(make_key(sha256="c" * 64), {**RECORD, "verdict": a.MODEL_FAILED})
    payload = json.loads(store.path().read_text())
    ids = {p["key"]["model_sha256"][0]: i for i, p in payload["proofs"].items()}
    payload["proofs"][ids["a"]]["checked_at_epoch"] -= 1000 * DAY      # too old for anything
    payload["proofs"][ids["c"]]["checked_at_epoch"] -= (a.MODEL_FAILED_DAYS + 1) * DAY  # a failure is forgotten sooner
    payload["proofs"][ids["b"]]["checked_at_epoch"] -= 1 * DAY
    store.path().write_text(json.dumps(payload))
    assert [p["key"]["model_sha256"][0] for p in store.entries()] == ["b"]
    assert store.entries(now=payload["proofs"][ids["b"]]["checked_at_epoch"] + 500 * DAY) == []


def test_a_second_section_in_its_own_file_is_independent_and_read_back_the_same_way(tmp_path):
    results = a.ProofStore(tmp_path / "benchmark-results.json", section="benchmarks")
    proofs = a.ProofStore(tmp_path / "acceptance-proofs.json")
    results.put(make_key(level="benchmark"), {"result": {"tokens_per_sec": 20.0}})
    assert json.loads(results.path().read_text())["benchmarks"] and "proofs" not in json.loads(results.path().read_text())
    assert results.get(make_key(level="benchmark"))["result"] == {"tokens_per_sec": 20.0}
    assert proofs.entries() == []
    # a file written under the other section name reads as empty, never as this one's entries
    assert a.ProofStore(results.path()).entries() == []
