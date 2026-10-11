"""The memory sampler: what it records, what it leaves null, and that it always stops.

Readings are fake. They are fed one at a time, so no test depends on timing,
except the ones that exist to prove the thread ends.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from potluck_local.services import llama_backend, memory_probe, windows_gpu
from potluck_local.services.memory_probe import GIB, MemorySampler, host_need_bytes


def feed(*values):
    """A reader that returns each value in turn, then the last one for ever. An exception is raised."""
    queue = list(values)

    def read():
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, BaseException):
            raise value
        return value

    return read


def sampled(available, rss=None, **kw):
    """A sampler with no thread; one reading is taken per sample() call."""
    sampler = MemorySampler(available=feed(*available), rss=feed(*(rss or [None])), threaded=False, **kw)
    sampler.start()
    return sampler


def wait_for(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert condition()


def sampler_threads():
    return [t for t in threading.enumerate() if t.name == "memory-sampler" and t.is_alive()]


# ---- the figure ----------------------------------------------------------------------


def test_free_memory_that_falls_and_recovers_reports_the_lowest_point():
    s = sampled([10 * GIB, 8 * GIB, 5 * GIB, 9 * GIB])
    for _ in range(3):
        s.sample()
    result = s.stop()  # the last reading (9 GiB) is the recovery
    assert result["available_before_bytes"] == 10 * GIB and result["available_min_bytes"] == 5 * GIB
    assert result["host_fall_bytes"] == 5 * GIB
    assert result["samples"] == 5  # the first reading, three more, the one stop() takes


def test_free_memory_that_never_falls_is_a_fall_of_zero_not_a_negative_number():
    s = sampled([6 * GIB, 7 * GIB, 9 * GIB])
    s.sample()
    s.sample()
    assert s.stop()["host_fall_bytes"] == 0


def test_the_baseline_is_the_reading_before_anything_loads_not_the_highest_one():
    s = sampled([6 * GIB, 9 * GIB, 7 * GIB])  # another program freed memory meanwhile
    s.sample()
    s.sample()
    assert s.stop()["host_fall_bytes"] == 0 and s.result()["available_before_bytes"] == 6 * GIB


def test_a_reader_that_raises_is_null_not_an_error():
    s = sampled([RuntimeError("psutil is gone")], rss=[OSError("denied")])
    s.sample()
    result = s.stop()
    assert result["host_fall_bytes"] is None and result["available_before_bytes"] is None
    assert result["rss_before_bytes"] is None and result["rss_peak_bytes"] is None


def test_a_reader_that_fails_after_the_start_keeps_what_it_read_before():
    s = sampled([10 * GIB, 7 * GIB, RuntimeError("gone")])
    s.sample()
    s.sample()
    assert s.stop()["host_fall_bytes"] == 3 * GIB


def test_a_baseline_that_could_not_be_read_is_never_made_up_from_a_later_reading():
    s = sampled([RuntimeError("once"), 8 * GIB, 4 * GIB])
    s.sample()
    s.sample()
    assert s.stop()["host_fall_bytes"] is None


def test_odd_values_from_a_reader_are_ignored():
    s = sampled([10 * GIB, "lots", None, True, 4 * GIB])
    for _ in range(3):
        s.sample()
    assert s.stop()["available_min_bytes"] == 4 * GIB


def test_the_engines_own_resident_memory_is_recorded_before_and_at_its_peak():
    s = sampled([10 * GIB], rss=[100, 900, 400])
    s.sample()
    s.sample()
    result = s.stop()
    assert (result["rss_before_bytes"], result["rss_peak_bytes"]) == (100, 900)


def test_the_real_readers_return_plain_integers():
    available, rss = memory_probe.read_available(), memory_probe.read_rss()
    assert isinstance(available, int) and available > 0 and isinstance(rss, int) and rss > 0


def test_the_real_readers_survive_a_missing_psutil(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)  # import raises
    assert memory_probe.read_available() is None and memory_probe.read_rss() is None


# ---- what the host is judged to need --------------------------------------------------


def test_host_need_is_the_larger_of_the_fall_and_the_engines_growth():
    assert host_need_bytes({"host_fall_bytes": 5 * GIB, "rss_before_bytes": 1 * GIB, "rss_peak_bytes": 3 * GIB}) == 5 * GIB
    # memory-mapped weights that still count as available: the fall reads low, the process does not
    assert host_need_bytes({"host_fall_bytes": 0, "rss_before_bytes": 1 * GIB, "rss_peak_bytes": 6 * GIB}) == 5 * GIB


def test_gpu_memory_counts_as_host_memory_only_where_the_gpu_shares_it():
    m = {"host_fall_bytes": 1 * GIB, "rss_before_bytes": 0, "rss_peak_bytes": 1 * GIB // 2, "gpu_bytes": 3 * GIB}
    assert host_need_bytes({**m, "gpu_shares_host": True}) == 3 * GIB  # unified memory: the Metal buffers are host memory
    assert host_need_bytes({**m, "gpu_shares_host": False}) == 1 * GIB  # a card with its own memory: not a host cost
    assert host_need_bytes(m) == 1 * GIB  # a measurement stored without the flag is not assumed to share


def test_host_need_uses_what_was_read_and_is_none_when_nothing_was():
    assert host_need_bytes({"host_fall_bytes": 2 * GIB, "rss_before_bytes": None, "rss_peak_bytes": None}) == 2 * GIB
    assert host_need_bytes({"host_fall_bytes": None, "rss_before_bytes": 1, "rss_peak_bytes": 10}) == 9
    assert host_need_bytes({"host_fall_bytes": None, "rss_before_bytes": None, "rss_peak_bytes": None}) is None
    assert host_need_bytes(None) is None and host_need_bytes("x") is None


# ---- one window across the load and the checks ----------------------------------------


def test_a_window_continues_from_the_loads_baseline_instead_of_taking_a_new_one():
    load = sampled([10 * GIB, 8 * GIB], rss=[100, 500])
    load.sample()
    first = load.stop()
    checks = MemorySampler(previous=first, available=feed(8 * GIB, 6 * GIB), rss=feed(500, 900), threaded=False).start()
    checks.sample()
    second = checks.stop()
    assert second["available_before_bytes"] == 10 * GIB and second["host_fall_bytes"] == 4 * GIB
    assert (second["rss_before_bytes"], second["rss_peak_bytes"]) == (100, 900)
    assert second["samples"] > first["samples"]


# ---- the thread always ends ------------------------------------------------------------


def test_stop_ends_the_thread_and_can_be_called_again():
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, interval=0.01).start()
    assert len(sampler_threads()) == 1
    first = s.stop()
    assert sampler_threads() == []
    assert s.stop()["samples"] == first["samples"]  # a second stop() changes nothing


def test_the_context_manager_stops_the_thread_when_the_body_raises():
    with pytest.raises(ValueError):
        with MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, interval=0.01):
            assert len(sampler_threads()) == 1
            raise ValueError("the load failed")
    assert sampler_threads() == []


def test_a_sampler_nobody_stops_ends_itself():
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, interval=0.01, max_seconds=0.05).start()
    deadline = time.monotonic() + 2
    while sampler_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sampler_threads() == []
    s.stop()


def test_a_thread_that_takes_samples_sees_a_fall_that_happens_while_it_runs():
    state = {"available": 9 * GIB}
    s = MemorySampler(available=lambda: state["available"], rss=lambda: 1, interval=0.005).start()
    state["available"] = 4 * GIB
    wait_for(lambda: s.result()["available_min_bytes"] == 4 * GIB)
    state["available"] = 9 * GIB  # recovered by the time it stops
    result = s.stop()
    assert result["host_fall_bytes"] == 5 * GIB and sampler_threads() == []


def test_a_reader_that_raises_inside_the_thread_does_not_kill_it_or_the_caller():
    reads = feed(8 * GIB, RuntimeError("x"))
    s = MemorySampler(available=reads, rss=lambda: 1, interval=0.005).start()
    wait_for(lambda: s.result()["samples"] >= 4)  # it kept going after the first failure
    assert s.stop()["available_before_bytes"] == 8 * GIB
    assert sampler_threads() == []


# ---- GPU memory, only where it can be read ----------------------------------------------

DEVICE = {"name": "MTL0", "description": "Apple M2 Pro", "type": "gpu", "free_bytes": 12 * GIB, "total_bytes": 16 * GIB}


def with_devices(monkeypatch, *snapshots):
    queue = list(snapshots)
    monkeypatch.setattr(llama_backend, "describe_devices", lambda *a: [dict(d) for d in (queue.pop(0) if len(queue) > 1 else queue[0])])


def test_off_windows_gpu_use_is_the_drop_in_the_devices_free_figure(monkeypatch):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
    meter = memory_probe.gpu_meter(DEVICE)
    with_devices(monkeypatch, [{**DEVICE, "free_bytes": 12 * GIB}], [{**DEVICE, "free_bytes": 9 * GIB}], [{**DEVICE, "free_bytes": 10 * GIB}])
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, threaded=False).start()
    s.sample(gpu=True)  # 3 GiB below the baseline
    result = s.stop()  # the last read is 2 GiB below it; the peak is kept
    assert result["gpu_bytes"] == 3 * GIB and result["gpu_source"] == "ggml_device_free"


@pytest.mark.parametrize("apple,kind,shares", [(True, "gpu", True), (False, "igpu", True), (False, "gpu", False)])
def test_the_result_says_whether_the_gpu_memory_is_the_machines_own(monkeypatch, apple, kind, shares):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
    monkeypatch.setattr(memory_probe, "_apple_silicon", lambda: apple)
    meter = memory_probe.gpu_meter({**DEVICE, "type": kind})
    with_devices(monkeypatch, [{**DEVICE, "type": kind, "free_bytes": 10 * GIB}])
    result = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, threaded=False).start().stop()
    assert result["gpu_bytes"] == 2 * GIB and result["gpu_shares_host"] is shares


def test_the_result_has_no_sharing_claim_without_a_gpu_figure():
    assert sampled([5 * GIB]).stop()["gpu_shares_host"] is None


def test_the_ggml_figure_is_read_by_the_loading_thread_only(monkeypatch):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
    meter = memory_probe.gpu_meter(DEVICE)
    assert meter.in_thread is False
    called = []
    monkeypatch.setattr(llama_backend, "describe_devices", lambda *a: called.append(threading.current_thread().name) or [DEVICE])
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, interval=0.005).start()
    wait_for(lambda: s.result()["samples"] >= 10)
    s.stop()
    assert called and set(called) == {threading.current_thread().name}


def test_a_device_that_reports_no_figure_gives_no_meter_and_a_null(monkeypatch):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
    assert memory_probe.gpu_meter({**DEVICE, "total_bytes": 0}) is None
    assert memory_probe.gpu_meter({**DEVICE, "free_bytes": 0}) is None
    assert memory_probe.gpu_meter(None) is None
    s = sampled([5 * GIB])
    assert s.stop()["gpu_bytes"] is None and s.result()["gpu_source"] is None


def test_a_device_that_vanishes_or_stops_reporting_after_the_load_is_null(monkeypatch):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: False)
    meter = memory_probe.gpu_meter(DEVICE)
    with_devices(monkeypatch, [])
    assert meter.used() is None
    with_devices(monkeypatch, [{**DEVICE, "total_bytes": 0}])
    assert meter.used() is None


def test_a_gpu_meter_that_raises_is_null_not_an_error(monkeypatch):
    class Broken:
        in_thread, source = False, "x"

        def used(self):
            raise OSError("driver reset")

    result = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=Broken(), threaded=False).start().stop()
    assert result["gpu_bytes"] is None


def windows(monkeypatch, own, status="ok", luid="0x00000000_0x0001"):
    monkeypatch.setattr(memory_probe, "_is_windows", lambda: True)
    monkeypatch.setattr(llama_backend, "system_gpu_use",
                        lambda device: SimpleNamespace(status=status, luid=luid, own_bytes=own.get("start", 0), detail="no match"))
    monkeypatch.setattr(windows_gpu, "pdh_dedicated_usage", lambda pid=None: ({luid: 99 * GIB}, own.get("now", {})))
    return luid


def test_on_windows_gpu_use_is_this_engines_own_share_less_what_it_held_at_the_start(monkeypatch):
    luid = windows(monkeypatch, {"start": 1 * GIB, "now": {"0x00000000_0x0001": 5 * GIB}})
    meter = memory_probe.gpu_meter(DEVICE)
    assert meter.source == "windows_pdh" and meter.in_thread is True
    assert meter.used() == 4 * GIB  # 5 now, 1 before the load; the 99 GiB everyone holds is not this engine's
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, threaded=False).start()
    assert s.stop()["gpu_bytes"] == 4 * GIB and luid


def test_on_windows_a_missing_per_process_entry_is_null_never_zero(monkeypatch):
    windows(monkeypatch, {"start": 0, "now": {}})
    meter = memory_probe.gpu_meter(DEVICE)
    assert meter.used() is None
    assert MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, threaded=False).start().stop()["gpu_bytes"] is None


def test_on_windows_the_counter_failing_is_null(monkeypatch):
    windows(monkeypatch, {"now": {"0x00000000_0x0001": 1}})

    def fail(pid=None):
        raise windows_gpu.WindowsGpuUnavailable("no counters")

    meter = memory_probe.gpu_meter(DEVICE)
    monkeypatch.setattr(windows_gpu, "pdh_dedicated_usage", fail)
    assert MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, threaded=False).start().stop()["gpu_bytes"] is None


def test_on_windows_an_adapter_that_cannot_be_matched_falls_back_to_the_ggml_figure(monkeypatch):
    windows(monkeypatch, {}, status="unavailable")
    meter = memory_probe.gpu_meter(DEVICE)
    assert meter.source == "ggml_device_free"


def test_on_windows_the_counter_is_read_in_the_sampler_thread_at_a_slower_pace(monkeypatch):
    windows(monkeypatch, {"now": {"0x00000000_0x0001": 2 * GIB}})
    reads = []
    real = windows_gpu.pdh_dedicated_usage
    monkeypatch.setattr(windows_gpu, "pdh_dedicated_usage", lambda pid=None: reads.append(1) or real(pid))
    meter = memory_probe.gpu_meter(DEVICE)
    s = MemorySampler(available=lambda: 5 * GIB, rss=lambda: 1, gpu=meter, interval=0.002).start()
    wait_for(lambda: len(reads) >= 2)
    result = s.stop()
    assert result["gpu_bytes"] == 2 * GIB
    assert 0 < len(reads) < result["samples"]  # sampled, but not on every tick
