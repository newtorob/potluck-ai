"""The recorded answer checks after a load: when they run, what they give way to, and where they end.

A load returns when the required checks have passed (the model is lent from then
on, the proof is stored). The recorded checks follow in the background, one at a
time, and give way to real work. The runtime is real. Only the native load is
replaced, as in test_acceptance_flow.py, and the model is a fake that records
what it was asked, in order, in one list shared with the test, so an order is
asserted from events and not from timing. Nothing here says anything about a
real model; the real runs are in the change notes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

import pytest

from potluck_local.services import acceptance as a
from potluck_local.services import acceptance_gates as g
from potluck_local.services import memory_probe
from potluck_local.services.runtime import RECORDED_TRIES, LLMRuntime, _Gate
from test_acceptance_checks import FakeModel
from test_acceptance_flow import SLUG, Rig
from test_acceptance_gates import THINK_TEMPLATE, Gated

REQUIRED = ["copy", "arithmetic", "repeats"]
# "stops" asks the model nothing, and "tools" is two turns.
RECORDED_ASKED = ["fact", "long_prompt", "reasoning", "tools", "tools"]


class Signal:
    """An event a worker thread sets and the test awaits."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()

    def set(self) -> None:
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self, timeout: float = 20.0) -> None:
        await asyncio.wait_for(self._event.wait(), timeout)


class Probe(Gated):
    """A thinking model with a tool template that writes down what it is asked.

    The first time it is asked a kind in `slow_once` (or the first `slow_times` times, for a kind in
    `slow_kinds`), its reply goes on and on, one chunk at a time, like a long generation. It tells the test
    when it has started (`generating`) and ends only when the reader stops reading it or it runs out.
    """

    def __init__(self, mode: str = "good", *, events: list[str], slow_kinds=(), slow_times: int = 1) -> None:
        super().__init__(THINK_TEMPLATE, mode=mode)
        self.events = events
        self.slow_kinds, self.slow_left = set(slow_kinds), {k: slow_times for k in slow_kinds}
        self.generating: dict[str, list[Signal]] = {}
        self.generated: dict[str, int] = {}
        self.asked_kinds: list[str] = []
        self.closed = False
        self.used_after_close: list[str] = []

    @staticmethod
    def kind_of(messages, tools) -> str:
        text = messages[0]["content"]
        for needle, kind in (("58213", "copy"), ("2 + 2", "arithmetic"), ("ocean", "repeats"), ("France", "fact"),
                             ("vault code", "long_prompt"), ("17 * 23", "reasoning")):
            if needle in text:
                return kind
        return "tools" if tools is not None else "request"

    def watch(self, kind: str, times: int = 1) -> list[Signal]:
        """One signal per time a slow reply of this kind starts generating, in order."""
        self.generating[kind] = [Signal() for _ in range(times)]
        return self.generating[kind]

    def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
        kind = self.kind_of(messages, tools)
        if self.closed:
            self.used_after_close.append(kind)
        self.asked_kinds.append(kind)
        self.events.append(f"model:{kind}:start")
        result = super().create_chat_completion(messages=messages, temperature=temperature, max_tokens=max_tokens,
                                                stream=False, tools=tools)
        if not stream:
            self.events.append(f"model:{kind}:end")
            return result
        return self._stream(kind, result)

    def _stream(self, kind, result):
        text = result["choices"][0]["message"]["content"]
        slow = self.slow_left.get(kind, 0) > 0
        if slow:
            self.slow_left[kind] -= 1
        pieces = re.findall(r"\S+\s*", text)
        try:
            yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
            for i, piece in enumerate(pieces):
                yield {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
                if slow and i == 0:
                    n = self.generated[kind] = self.generated.get(kind, 0) + 1
                    if kind in self.generating and n <= len(self.generating[kind]):
                        self.generating[kind][n - 1].set()
                    for _ in range(3000):  # about six seconds if nobody stops it
                        time.sleep(0.002)
                        yield {"choices": [{"delta": {"content": "more "}, "finish_reason": None}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        finally:
            self.events.append(f"model:{kind}:closed")

    def close(self) -> None:
        self.closed = True
        self.events.append("model:unloaded")


def rig_with_probe(tmp_path, monkeypatch, *, events, slow_kinds=(), slow_times=1, **kw) -> Rig:
    rig = Rig(tmp_path, monkeypatch, **kw)
    rig.make_model = lambda mode: Probe(mode, events=events, slow_kinds=slow_kinds, slow_times=slow_times)
    return rig


async def load(rig: Rig, path=None) -> dict:
    return await rig.rt.load_model_async(str(path or rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True)


async def until(predicate, timeout: float = 10.0) -> None:
    """Wait for a condition that other code on the loop (or a thread) makes true. Yields, never sleeps."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await asyncio.sleep(0)


def ask(rt: LLMRuntime):
    """A request, as the chat route makes it: it takes the inference gate and asks the model."""
    return asyncio.ensure_future(rt.chat([{"role": "user", "content": "Say hello in four words."}], max_tokens=24))


def proof_of(rig: Rig, **match) -> dict:
    found = [p for p in rig.proofs().values() if all(p["key"].get(k) == v for k, v in match.items())]
    assert len(found) == 1, found
    return found[0]


def ids(checks) -> list[str]:
    return [c["id"] for c in checks]


def asyncio_run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def events() -> list[str]:
    return []


# ---- the load returns with the verdict ------------------------------------------------


def test_the_load_returns_after_the_required_checks_with_the_verdict_published(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)

    async def scenario():
        result = await load(rig)
        # Nothing else has run since the load returned: this is what the caller sees first.
        s = rig.summary()
        assert (s["status"], s["verdict"], s["from_proof"], s["recorded"]) == ("passed", "ok", False, "running")
        assert ids(s["checks"]) == REQUIRED and all(c["required"] and c["passed"] for c in s["checks"])
        assert rig.rt.answers_verified() is True and a.lending_allowed(rig.rt) is True
        assert rig.models[-1].asked_kinds == REQUIRED
        proof = proof_of(rig)
        assert proof["verdict"] == "ok" and ids(proof["checks"]) == REQUIRED
        assert result["n_gpu_layers"] == -1
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())


def test_the_recorded_checks_then_run_and_the_result_and_the_proof_gain_them(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)

    async def scenario():
        await load(rig)
        before = rig.summary()
        stored = proof_of(rig)
        await rig.rt.wait_for_recorded()
        return before, stored

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        before, stored = asyncio_run(scenario())
    after = rig.summary()
    every = REQUIRED + ["fact", "stops", "long_prompt", "reasoning", "tools"]
    assert (after["status"], after["verdict"], after["recorded"]) == ("passed", "ok", "done")
    assert ids(after["checks"]) == every and all(c["passed"] for c in after["checks"])
    assert not [c for c in after["checks"] if c.get("skipped")], "a thinking model with a tool template skips none"
    assert rig.models[-1].asked_kinds == REQUIRED + RECORDED_ASKED
    assert after["checked_at"] == before["checked_at"], "the time of the check is the verdict's"
    proof = proof_of(rig)
    assert ids(proof["checks"]) == every and proof["verdict"] == "ok"
    assert proof["checked_at_epoch"] == stored["checked_at_epoch"] and proof["duration_ms"] >= stored["duration_ms"]
    assert proof["output_tokens"] > stored["output_tokens"]
    assert all(len(c["excerpt"]) <= 80 for c in proof["checks"])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("answer_check")]
    assert lines[0].startswith("answer_check status=passed") and "checks_passed=3/3" in lines[0]
    assert lines[1].startswith("answer_check_recorded status=done") and "checks_passed=5/5" in lines[1]
    assert len(lines) == 2


def test_a_good_load_is_lent_before_any_recorded_check_has_run(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["fact"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        await probe.watch("fact")[0].wait()
        # A recorded check is generating right now, and the machine is verified and not busy.
        assert rig.summary()["recorded"] == "running" and rig.rt.answers_verified() is True
        assert a.lending_allowed(rig.rt) and rig.rt._inference_lock.locked() and not rig.rt.busy()
        request = ask(rig.rt)
        await until(lambda: rig.rt.busy())
        await request
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())


# ---- a request goes first ----------------------------------------------------------------


def test_a_request_that_is_running_when_a_check_wants_to_start_goes_first(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)

    async def scenario():
        await load(rig)
        gate = rig.rt._inference_lock
        await gate.acquire()  # a request in flight, taken before the background task has had a turn
        events.append("request:running")
        await until(lambda: rig.rt._recorded_watchers)  # the checks are now waiting for it
        assert "model:fact:start" not in events
        second = ask(rig.rt)
        await until(lambda: gate.waiting == 1)
        events.append("second:queued")
        gate.release()
        events.append("request:released")
        await second
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    order = [e for e in events if e in ("request:running", "second:queued", "request:released", "model:request:start",
                                        "model:fact:start", "model:long_prompt:start")]
    assert order == ["request:running", "second:queued", "request:released", "model:request:start",
                     "model:fact:start", "model:long_prompt:start"]
    assert rig.summary()["recorded"] == "done" and not [c for c in rig.summary()["checks"] if c.get("skipped")]


def test_a_request_that_arrives_between_two_checks_goes_before_the_next_one(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    spawned: list[asyncio.Future] = []

    async def scenario():
        await load(rig)
        real = rig.rt._apply_step

        def spy(job, step_id, outcome):
            real(job, step_id, outcome)
            if step_id == "fact":  # the request is on its way as this check's result is taken in
                events.append("request:sent")
                spawned.append(ask(rig.rt))

        rig.rt._apply_step = spy
        await rig.rt.wait_for_recorded()
        await spawned[0]

    asyncio_run(scenario())
    order = [e for e in events if e in ("model:fact:closed", "request:sent", "model:request:start", "model:request:end",
                                        "model:long_prompt:start")]
    assert order == ["model:fact:closed", "request:sent", "model:request:start", "model:request:end",
                     "model:long_prompt:start"]
    assert not [c for c in rig.summary()["checks"] if c.get("skipped")], "no check was cut short"


def test_a_request_that_arrives_while_a_check_is_generating_stops_it_and_goes_first(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        job = rig.rt._recorded_job
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        gate = rig.rt._inference_lock
        real_wait = gate._on_wait

        def noting_wait():
            events.append("request:waiting")
            real_wait()

        gate._on_wait = noting_wait
        await generating.wait()
        events.append("check:generating")
        await ask(rig.rt)
        await rig.rt.wait_for_recorded()
        return job

    job = asyncio_run(scenario())
    order = [e for e in events if e in ("model:reasoning:start", "check:generating", "request:waiting",
                                        "model:reasoning:closed", "model:request:start", "model:request:end")]
    # The first reasoning attempt is stopped by the request, which runs; the check is asked again afterwards.
    assert order == ["model:reasoning:start", "check:generating", "request:waiting", "model:reasoning:closed",
                     "model:request:start", "model:request:end", "model:reasoning:start", "model:reasoning:closed"]
    s = rig.summary()
    assert s["recorded"] == "done"
    reasoning = [c for c in s["checks"] if c["id"] == "reasoning"]
    assert reasoning == [{"id": "reasoning", "passed": True, "required": False}], "the second attempt finished"
    assert rig.models[-1].asked_kinds.count("reasoning") == RECORDED_TRIES
    assert job.attempts == 6, "five checks, and the one a request stopped asked again"


def test_a_check_cut_short_every_time_is_recorded_as_skipped_because_the_machine_was_busy(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"], slow_times=RECORDED_TRIES)

    async def scenario():
        await load(rig)
        for generating in rig.models[-1].watch("reasoning", RECORDED_TRIES):
            await generating.wait()
            await ask(rig.rt)
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    s = rig.summary()
    assert s["recorded"] == "done"
    assert [c for c in s["checks"] if c["id"] == "reasoning"] == [
        {"id": "reasoning", "passed": True, "required": False, "skipped": "the machine was busy"}]
    assert s["status"] == "passed" and rig.rt.answers_verified() is True, "a busy machine is not a failed one"
    stored = [c for c in proof_of(rig)["checks"] if c["id"] == "reasoning"]
    assert stored[0]["skipped"] == "the machine was busy" and stored[0]["passed"] is True
    assert ids(s["checks"])[-2:] == ["reasoning", "tools"], "the checks after it still ran"


def test_a_check_cut_short_is_run_again_by_the_next_load(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"], slow_times=RECORDED_TRIES)

    async def scenario():
        await load(rig)
        for generating in rig.models[-1].watch("reasoning", RECORDED_TRIES):
            await generating.wait()
            await ask(rig.rt)
        await rig.rt.wait_for_recorded()
        await rig.rt.unload_model_async()
        events.clear()
        rig.make_model = lambda mode: Probe(mode, events=events)  # not slow this time
        await load(rig)
        s = rig.summary()
        assert (s["from_proof"], s["recorded"]) == (True, "running"), "the verdict is the proof's, at once"
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    assert rig.models[-1].asked_kinds == ["reasoning"], "only the check that was cut short asks again"
    s = rig.summary()
    assert s["recorded"] == "done" and not [c for c in s["checks"] if c.get("skipped")]
    assert [c["passed"] for c in proof_of(rig)["checks"]] == [True] * 8


# ---- it stops cleanly -----------------------------------------------------------------------


def _problems(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_unloading_during_a_check_stops_it_first_and_never_uses_the_freed_model(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        task = rig.rt._recorded_task
        await generating.wait()
        await rig.rt.unload_model_async()
        await task  # the background task has ended, without raising
        return probe

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        probe = asyncio_run(scenario())
    order = [e for e in events if e in ("model:reasoning:start", "model:reasoning:closed", "model:unloaded")]
    assert order == ["model:reasoning:start", "model:reasoning:closed", "model:unloaded"]
    assert probe.used_after_close == [] and rig.rt._recorded_job is None
    assert _problems(caplog) == []
    assert (rig.summary()["status"], rig.summary()["recorded"]) == ("not_run", None)
    proof = proof_of(rig)
    assert proof["verdict"] == "ok" and ids(proof["checks"])[:5] == REQUIRED + ["fact", "stops"]
    ended = [r.getMessage() for r in caplog.records if r.getMessage().startswith("answer_check_recorded")]
    assert len(ended) == 1 and "status=stopped" in ended[0] and "reason=the model was unloaded" in ended[0]


def test_a_synchronous_unload_waits_for_the_check_that_is_generating(tmp_path, monkeypatch, events, caplog):
    """The app's own shutdown calls unload_model() on the event loop's thread pool without the gates."""
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        task = rig.rt._recorded_task
        await generating.wait()
        await asyncio.to_thread(rig.rt.unload_model)
        await task
        return probe

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        probe = asyncio_run(scenario())
    order = [e for e in events if e in ("model:reasoning:closed", "model:unloaded")]
    assert order == ["model:reasoning:closed", "model:unloaded"]
    assert probe.used_after_close == [] and _problems(caplog) == []
    assert proof_of(rig)["verdict"] == "ok"


def test_loading_another_model_during_a_check_stops_it_and_the_new_model_gets_its_own(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])
    other = tmp_path / "models" / "other" / "model.gguf"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"another model entirely")

    async def scenario():
        await load(rig)
        first = rig.models[-1]
        generating = first.watch("reasoning")[0]
        task = rig.rt._recorded_task
        await generating.wait()
        rig.make_model = lambda mode: Probe(mode, events=events)  # the second model is not slow
        await load(rig, other)
        await task
        second = rig.models[-1]
        assert second is not first and rig.summary()["model_id"] == "other"
        assert rig.summary()["recorded"] == "running", "the new model's checks are its own"
        await rig.rt.wait_for_recorded()
        return first

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        first = asyncio_run(scenario())
    assert events.index("model:reasoning:closed") < events.index("model:unloaded")
    assert first.used_after_close == [] and _problems(caplog) == []
    s = rig.summary()
    assert (s["model_id"], s["status"], s["recorded"]) == ("other", "passed", "done") and len(s["checks"]) == 8
    keys = {p["key"]["model_sha256"]: p for p in rig.proofs().values()}
    assert len(keys) == 2 and all(p["verdict"] == "ok" for p in keys.values())
    partial = [p for p in keys.values() if len(p["checks"]) < 8]
    assert len(partial) == 1 and ids(partial[0]["checks"])[:3] == REQUIRED


def test_the_engine_stopping_during_a_check_leaves_a_stopped_result_that_can_still_be_read(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        await generating.wait()
        # The scenario ends here, with the check generating: asyncio.run cancels what is left.

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        asyncio_run(scenario())
    probe = rig.models[-1]
    s = rig.summary()
    assert (s["status"], s["verdict"], s["recorded"]) == ("passed", "ok", "stopped")
    assert ids(s["checks"])[:5] == REQUIRED + ["fact", "stops"] and rig.rt.answers_verified() is True
    assert "model:reasoning:closed" in events and probe.used_after_close == [] and not probe.closed
    assert _problems(caplog) == []
    assert proof_of(rig)["verdict"] == "ok" and "fact" in ids(proof_of(rig)["checks"])
    assert rig.rt._background_holds is False and not rig.rt._inference_lock.locked()
    assert not [t for t in threading.enumerate() if t.name == "memory-sampler" and t.is_alive()]


def test_the_engine_stopping_while_the_checks_wait_for_a_request_ends_them_without_noise(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)

    async def scenario():
        await load(rig)
        await rig.rt._inference_lock.acquire()  # a request that never finishes before the engine stops
        await until(lambda: rig.rt._recorded_watchers)

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        asyncio_run(scenario())
    assert rig.summary()["recorded"] == "stopped" and rig.models[-1].asked_kinds == REQUIRED
    assert _problems(caplog) == []


def test_a_new_check_of_the_same_model_takes_over_from_the_running_one(tmp_path, monkeypatch, events, caplog):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        task = rig.rt._recorded_task
        await generating.wait()
        report, _ = await rig.rt.accept_loaded_async(expected_model=SLUG, expected_sha256=None, level="quick", force=True)
        await task
        return report

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        report = asyncio_run(scenario())
    assert report["recorded"] == "done" and len(report["checks"]) == 8 and report["from_proof"] is False
    assert rig.summary() == report and _problems(caplog) == []
    assert len(proof_of(rig)["checks"]) == 8
    ended = [r.getMessage() for r in caplog.records if r.getMessage().startswith("answer_check_recorded")]
    assert len(ended) == 1 and "reason=superseded" in ended[0]


def test_turning_the_check_off_ends_the_recorded_checks(tmp_path, monkeypatch, events):
    from potluck_local.services.settings_manager import get_settings, save_settings

    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        generating = probe.watch("reasoning")[0]
        await generating.wait()
        settings = get_settings().model_copy(deep=True)
        settings.runtime.verify_answers = False
        save_settings(settings)
        await ask(rig.rt)  # the check gives way, and is not asked again
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    assert rig.models[-1].asked_kinds.count("reasoning") == 1 and "tools" not in rig.models[-1].asked_kinds
    assert rig.rt._recorded_job is None


# ---- nothing is lost: proofs ---------------------------------------------------------------------


def test_a_proof_without_recorded_results_gives_the_verdict_at_once_and_one_background_run(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    rig.load(settle=False)  # a verdict, stored with the required checks only
    rig.rt.unload_model()  # the recorded checks never ran
    assert ids(proof_of(rig)["checks"]) == REQUIRED

    async def scenario():
        await load(rig)
        probe = rig.models[-1]
        s = rig.summary()
        assert (s["status"], s["from_proof"], s["recorded"]) == ("passed", True, "running")
        assert ids(s["checks"]) == REQUIRED and rig.rt.answers_verified() is True
        assert probe.asked_kinds == [], "the verdict did not ask the model anything"
        await rig.rt.wait_for_recorded()
        return probe

    probe = asyncio_run(scenario())
    assert probe.asked_kinds == RECORDED_ASKED, "only what was missing"
    s = rig.summary()
    assert (s["from_proof"], s["recorded"], len(s["checks"])) == (True, "done", 8)
    assert len(proof_of(rig)["checks"]) == 8

    async def again():
        rig.rt.unload_model()
        before = rig.rt._recorded_task
        await load(rig)
        assert rig.summary()["recorded"] == "done" and rig.rt._recorded_job is None
        assert rig.rt._recorded_task is before, "no second run"
        assert rig.models[-1].asked_kinds == []

    asyncio_run(again())


def test_a_proof_stopped_part_way_runs_only_the_rest(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def first():
        await load(rig)
        await rig.models[-1].watch("reasoning")[0].wait()
        await rig.rt.unload_model_async()

    asyncio_run(first())
    stored = ids(proof_of(rig)["checks"])
    assert stored == REQUIRED + ["fact", "stops", "long_prompt"]

    rig.make_model = lambda mode: Probe(mode, events=events)  # not slow this time

    async def second():
        events.clear()
        await load(rig)
        await rig.rt.wait_for_recorded()

    asyncio_run(second())
    assert rig.models[-1].asked_kinds == ["reasoning", "tools", "tools"], "the checks that had finished are not asked again"
    assert len(proof_of(rig)["checks"]) == 8 and rig.summary()["recorded"] == "done"


def test_a_failing_required_check_never_starts_the_recorded_ones(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, gpu="doubled", cpu="doubled")

    async def scenario():
        await load(rig)
        assert rig.rt._recorded_task is None and rig.rt._recorded_job is None
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    s = rig.summary()
    assert (s["status"], s["verdict"], s["recorded"]) == ("failed", "model_failed", "stopped")
    assert rig.loads == [(-1, False), (0, True), (-1, False)], "the CPU cross-check and the put-back, as before"
    assert [m.asked_kinds for m in rig.models] == [REQUIRED, REQUIRED, []]
    assert ids(s["checks"]) == REQUIRED and rig.rt.answers_verified() is False
    assert all(ids(p["checks"]) == REQUIRED for p in rig.proofs().values())


def test_a_gpu_that_gives_wrong_answers_falls_back_to_the_cpu_and_the_cpu_gets_the_recorded_checks(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, gpu="doubled")

    async def scenario():
        await load(rig)
        s = rig.summary()
        assert rig.loads == [(-1, False), (0, True)]
        assert (s["status"], s["verdict"], s["recorded"]) == ("passed", "gpu_wrong_output", "running")
        assert s["fallback"] == {"from": "gpu", "to": "cpu", "reason": "gpu_wrong_output"}
        assert rig.rt.answers_verified() is True and ids(s["checks"]) == REQUIRED
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    gpu_model, cpu_model = rig.models
    assert gpu_model.asked_kinds == REQUIRED, "the GPU copy is never asked for more"
    assert cpu_model.asked_kinds == REQUIRED + ["fact"], "a CPU quick check skips the long prompt, reasoning and tools"
    s = rig.summary()
    assert s["recorded"] == "done" and ids(s["checks"]) == REQUIRED + ["fact", "stops", "reasoning", "tools"]
    verdicts = {p["key"]["device_kind"]: (p["verdict"], len(p["checks"])) for p in rig.proofs().values()}
    assert verdicts == {"gpu": ("gpu_wrong_output", 3), "cpu": ("ok", 7)}


def test_the_next_load_of_a_known_bad_gpu_goes_straight_to_the_cpu_and_finishes_its_checks(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, gpu="doubled")

    async def first():
        await load(rig)  # the cross-check; its recorded checks are stopped by the unload below
        await rig.rt.unload_model_async()

    asyncio_run(first())
    rig.loads.clear()

    async def second():
        await load(rig)
        s = rig.summary()
        assert rig.loads == [(0, True)] and (s["from_proof"], s["fallback"] is not None) == (True, True)
        await rig.rt.wait_for_recorded()

    asyncio_run(second())
    assert rig.summary()["recorded"] == "done" and len(rig.summary()["checks"]) == 7


# ---- an explicit test does everything before it answers -------------------------------------------


def test_the_explicit_test_still_waits_for_every_check_and_returns_the_whole_result(tmp_path, monkeypatch, events):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from potluck_local.routers import health, models

    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    rig.load(settle=False)  # only the verdict, with the recorded checks still to come
    assert rig.summary()["recorded"] == "running" and len(rig.summary()["checks"]) == 3
    for module in (health, models):
        monkeypatch.setattr(module, "get_runtime", lambda: rig.rt)
    app = FastAPI()
    app.include_router(models.router, prefix="/models")
    client = TestClient(app)
    body = client.post(f"/models/{SLUG}/test", json={}).json()
    acceptance = body["acceptance"]
    assert body["status"] == "passed" and acceptance["recorded"] == "done"
    assert ids(acceptance["checks"]) == REQUIRED + ["fact", "stops", "long_prompt", "reasoning", "tools"]
    assert rig.rt._recorded_job is None and len(proof_of(rig)["checks"]) == 8
    assert isinstance(body["outputTokens"], int) and body["outputTokens"] > 0
    forced = client.post(f"/models/{SLUG}/test", json={"force": True}).json()["acceptance"]
    assert forced["recorded"] == "done" and len(forced["checks"]) == 8 and forced["from_proof"] is False
    full = client.post(f"/models/{SLUG}/test", json={"level": "full"}).json()["acceptance"]
    assert full["recorded"] == "done" and full["level"] == "full" and len(full["checks"]) == 8


def test_the_explicit_test_of_a_proof_that_lacks_recorded_results_runs_them_now(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    rig.load(settle=False)
    rig.rt.unload_model()
    rig.load(settle=False)  # the verdict from the proof; the recorded checks are still to come
    probe = rig.models[-1]
    assert probe.asked_kinds == [] and rig.summary()["recorded"] == "running"
    report, tokens = rig.check()
    assert report["from_proof"] is True and report["recorded"] == "done" and len(report["checks"]) == 8
    assert probe.asked_kinds == RECORDED_ASKED and tokens and tokens > 0
    assert len(proof_of(rig)["checks"]) == 8 and rig.rt._recorded_job is None


# ---- the contract ------------------------------------------------------------------------------------


def test_recorded_is_null_when_no_check_covers_the_load_or_the_check_is_off(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    assert rig.summary()["recorded"] is None
    rig.load(verify=False)
    assert (rig.summary()["status"], rig.summary()["recorded"]) == ("skipped", None)
    rig.rt.unload_model()
    rig.load(rpc_servers=["100.64.0.6:50052"])
    assert (rig.summary()["status"], rig.summary()["recorded"]) == ("not_run", None)


def test_the_summary_keeps_every_field_and_adds_recorded_last(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)

    async def scenario():
        await load(rig)
        running = rig.summary()
        await rig.rt.wait_for_recorded()
        return running

    running = asyncio_run(scenario())
    old = ["schema_version", "status", "model_id", "level", "checked_at", "from_proof", "ran_on", "checks", "verdict",
           "fallback", "message"]
    assert list(running) == old + ["recorded"] and list(rig.summary()) == old + ["recorded"]
    assert list(running["checks"][0]) == ["id", "passed", "required"]
    assert list(a.summary()) == old + ["recorded"]


def test_health_inference_shows_the_result_filling_in(tmp_path, monkeypatch, events):
    from potluck_local.routers import health

    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    monkeypatch.setattr(health, "get_runtime", lambda: rig.rt)

    async def scenario():
        await load(rig)
        mid = (await health.inference_diagnostics())["acceptance"]
        await rig.rt.wait_for_recorded()
        end = (await health.inference_diagnostics())["acceptance"]
        return mid, end

    mid, end = asyncio_run(scenario())
    assert (mid["status"], mid["recorded"], len(mid["checks"])) == ("passed", "running", 3)
    assert (end["status"], end["recorded"], len(end["checks"])) == ("passed", "done", 8)


def test_the_machine_is_not_busy_because_of_a_recorded_check_but_is_for_a_request(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, slow_kinds=["reasoning"])

    async def scenario():
        rt = rig.rt
        assert rt.busy() is False
        await load(rig)
        await rig.models[-1].watch("reasoning")[0].wait()
        assert rt._inference_lock.locked() and rt.busy() is False
        request = ask(rt)
        await until(lambda: rt._inference_lock.waiting == 1)
        assert rt.busy() is True
        await request
        await rt.wait_for_recorded()
        assert rt.busy() is False
        await rt._inference_lock.acquire()
        assert rt.busy() is True
        rt._inference_lock.release()

    asyncio_run(scenario())


# ---- the gate -------------------------------------------------------------------------------------------


def test_the_gate_counts_who_waits_and_says_so_only_when_it_is_held():
    async def scenario():
        told: list[str] = []
        changed: list[int] = []
        gate = _Gate(on_wait=lambda: told.append("wait"), on_change=lambda: changed.append(1))
        await gate.acquire()
        assert told == [] and gate.waiting == 0, "a free gate is taken without a word"
        first = asyncio.ensure_future(gate.acquire())
        await until(lambda: gate.waiting == 1)
        assert told == ["wait"]
        gate.release()
        await first
        assert gate.waiting == 0 and gate.locked() and changed
        gate.release()
        assert not gate.locked()

    asyncio_run(scenario())


def test_a_waiter_that_gives_up_wakes_the_checks():
    async def scenario():
        woken: list[int] = []
        gate = _Gate(on_change=lambda: woken.append(1))
        await gate.acquire()
        waiter = asyncio.ensure_future(gate.acquire())
        await until(lambda: gate.waiting == 1)
        woken.clear()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate.waiting == 0 and woken, "nothing else would tell the checks that nobody waits any more"

    asyncio_run(scenario())


# ---- what a check adds to the measurement ----------------------------------------------------------------


GIB = 1024 ** 3


def measurement(**kw) -> dict:
    base = {"host_fall_bytes": 3 * GIB, "available_before_bytes": 10 * GIB, "available_min_bytes": 7 * GIB,
            "rss_before_bytes": GIB // 4, "rss_peak_bytes": 2 * GIB, "gpu_bytes": 2 * GIB, "gpu_source": "ggml_device_free",
            "gpu_shares_host": True, "samples": 10, "seconds": 1.0, "window": "load+required"}
    return {**base, **kw}


def test_widen_adds_the_largest_increment_on_top_of_the_load_and_never_lowers_a_figure():
    got = memory_probe.widen(measurement(), [
        {"host_fall_bytes": GIB // 2, "rss_peak_bytes": 2 * GIB + 5, "gpu_added_bytes": 0, "samples": 4, "seconds": 0.5},
        {"host_fall_bytes": GIB, "rss_peak_bytes": GIB, "gpu_added_bytes": GIB // 4, "samples": 6, "seconds": 2.0},
    ])
    assert got["host_fall_bytes"] == 4 * GIB and got["available_min_bytes"] == 6 * GIB
    assert got["available_before_bytes"] == 10 * GIB, "the baseline stays the load's"
    assert got["rss_peak_bytes"] == 2 * GIB + 5 and got["gpu_bytes"] == 2 * GIB + GIB // 4
    assert (got["samples"], got["seconds"], got["window"]) == (20, 3.5, "load+checks")
    assert memory_probe.widen(measurement(), [{"host_fall_bytes": 0, "rss_peak_bytes": 1, "gpu_added_bytes": 0}]) \
        ["host_fall_bytes"] == 3 * GIB


def test_widen_leaves_a_figure_that_was_not_measured_null_and_does_nothing_without_increments():
    m = measurement(host_fall_bytes=None, available_min_bytes=None, gpu_bytes=None, gpu_source=None)
    got = memory_probe.widen(m, [{"host_fall_bytes": GIB, "rss_peak_bytes": 1, "gpu_added_bytes": GIB}])
    assert got["host_fall_bytes"] is None and got["gpu_bytes"] is None and got["available_min_bytes"] is None
    assert memory_probe.widen(measurement(), []) == measurement()
    assert memory_probe.widen(None, [{"host_fall_bytes": GIB}]) is None


def test_widen_floors_free_memory_at_zero():
    got = memory_probe.widen(measurement(available_min_bytes=GIB // 2), [{"host_fall_bytes": GIB}])
    assert got["available_min_bytes"] == 0


def test_an_increment_reports_only_what_happened_while_it_ran():
    state = {"available": 10 * GIB, "rss": GIB, "gpu_used": 5 * GIB}

    class Meter:
        source, in_thread, shares_host_memory = "test", False, True

        def used(self):
            return state["gpu_used"]

    state["available"] -= 2 * GIB  # memory another program took before the check began: not the check's
    inc = memory_probe.Increment(gpu=Meter(), threaded=False, available=lambda: state["available"],
                                 rss=lambda: state["rss"])
    inc.start()
    state["available"] -= GIB // 2
    state["rss"] += GIB // 4
    state["gpu_used"] += GIB // 8
    got = inc.stop()
    assert got["host_fall_bytes"] == GIB // 2 and got["rss_peak_bytes"] == GIB + GIB // 4
    assert got["gpu_added_bytes"] == GIB // 8


def _machine_rig(tmp_path, monkeypatch, events, slow_kinds=()):
    """The measurement test rig (a fake machine whose memory the sampler reads), with the thinking probe model."""
    import llama_cpp

    import test_measurement_flow as mf

    rig = mf.Rig(tmp_path, monkeypatch)

    class MeasuredProbe(mf.FakeLlama):
        def __init__(self, model_path, n_ctx=512, n_gpu_layers=0, n_threads=None, verbose=True, **kw):
            super().__init__(model_path, n_ctx, n_gpu_layers, n_threads, verbose, **kw)
            self._probe = Probe(self.mode, events=events, slow_kinds=slow_kinds)
            self.metadata = self._probe.metadata

        def create_chat_completion(self, *, messages, temperature, max_tokens, stream, tools=None):
            text = messages[0]["content"]
            if "vault code" in text:  # the long prompt needs buffers that the first answer did not
                more = {"available": self.machine.long_prompt_cost, "rss": 0,
                        "gpu_free": self.machine.long_prompt_gpu if self.on_gpu else 0}
                self._take(more)
                self.held = {k: self.held[k] + more[k] for k in more}
            elif "17 * 23" in text and self.machine.reasoning_cost:  # only the first attempt needs them
                more = {"available": self.machine.reasoning_cost, "rss": 0, "gpu_free": 0}
                self.machine.reasoning_cost = 0
                self._take(more)
                self.held = {k: self.held[k] + more[k] for k in more}
            elif not self._answered:
                self._answered = True
                more = {"available": self.machine.check_cost, "rss": 0,
                        "gpu_free": self.machine.check_cost // 2 if self.on_gpu else 0}
                self._take(more)
                self.held = {k: self.held[k] + more[k] for k in more}
            return self._probe.create_chat_completion(messages=messages, temperature=temperature,
                                                      max_tokens=max_tokens, stream=stream, tools=tools)

        def tokenize(self, data, add_bos=True, special=False):
            return self._probe.tokenize(data, add_bos, special)

        def detokenize(self, ids_):
            return self._probe.detokenize(ids_)

        def reset(self):
            pass

    rig.machine.long_prompt_cost = 0
    rig.machine.long_prompt_gpu = 0
    rig.machine.reasoning_cost = 0
    monkeypatch.setattr(llama_cpp, "Llama", MeasuredProbe)
    return rig, mf


def test_the_long_prompt_check_adds_its_memory_to_the_stored_measurement_when_it_runs(tmp_path, monkeypatch, events):
    rig, mf = _machine_rig(tmp_path, monkeypatch, events)
    rig.machine.long_prompt_cost, rig.machine.long_prompt_gpu = GIB, GIB // 2

    async def scenario():
        await rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True)
        at_verdict = dict(rig.proof()["measurement"])
        await rig.rt.wait_for_recorded()
        return at_verdict

    at_verdict = asyncio_run(scenario())
    # The verdict's figure is the load plus the first answer: 3 GiB and half a GiB, as when the checks ran inside the load.
    assert at_verdict["host_fall_bytes"] == 3 * GIB + GIB // 2 and at_verdict["window"] == "load+required"
    assert at_verdict["gpu_bytes"] == 2 * GIB + GIB // 4
    final = rig.proof()["measurement"]
    assert final["host_fall_bytes"] == 4 * GIB + GIB // 2, "plus the GiB the long prompt took"
    assert final["gpu_bytes"] == 2 * GIB + GIB // 4 + GIB // 2 and final["window"] == "load+checks"
    assert final["available_before_bytes"] == 10 * GIB and final["n_ctx"] == 4096
    assert final["samples"] > at_verdict["samples"]
    assert rig.rt._load_memory["host_fall_bytes"] == 4 * GIB + GIB // 2, "the window the next explicit check extends"
    assert mf.sampler_threads() == []


def test_memory_that_other_programs_took_before_a_check_ran_is_not_counted_as_the_models(tmp_path, monkeypatch, events):
    rig, mf = _machine_rig(tmp_path, monkeypatch, events)

    async def scenario():
        await rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True)
        at_verdict = dict(rig.proof()["measurement"])
        rig.machine.available -= 2 * GIB  # a browser starts while the checks wait for their turn
        await rig.rt.wait_for_recorded()
        return at_verdict

    at_verdict = asyncio_run(scenario())
    final = rig.proof()["measurement"]
    assert final["host_fall_bytes"] == at_verdict["host_fall_bytes"] == 3 * GIB + GIB // 2
    assert final["available_before_bytes"] == 10 * GIB


def test_every_attempt_is_measured_and_the_largest_reading_counts(tmp_path, monkeypatch, events):
    rig, mf = _machine_rig(tmp_path, monkeypatch, events)
    rig.machine.long_prompt_cost = GIB // 2
    seen: list[dict] = []
    real_stop = memory_probe.Increment.stop

    def spy(self):
        first = self._result is None
        got = real_stop(self)
        if first:
            seen.append(got)
        return got

    monkeypatch.setattr(memory_probe.Increment, "stop", spy)

    async def scenario():
        await rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True)
        job = rig.rt._recorded_job
        await rig.rt.wait_for_recorded()
        return job

    job = asyncio_run(scenario())
    assert job.attempts == len(seen) == len(job.increments) == 5, "one reading per attempt, stops included"
    assert max(s["host_fall_bytes"] or 0 for s in seen) == GIB // 2


def test_the_memory_an_attempt_used_counts_even_when_a_request_stopped_it(tmp_path, monkeypatch, events):
    rig, mf = _machine_rig(tmp_path, monkeypatch, events, slow_kinds=["reasoning"])
    rig.machine.reasoning_cost = GIB // 4  # the first reasoning attempt takes it; the one after the request does not

    async def scenario():
        await rig.rt.load_model_async(str(rig.path), n_ctx=4096, n_gpu_layers=-1, verify_answers=True)
        job = rig.rt._recorded_job
        generating = rig.rt._model._probe.watch("reasoning")[0]
        await generating.wait()
        await ask(rig.rt)
        await rig.rt.wait_for_recorded()
        return job

    job = asyncio_run(scenario())
    assert job.attempts == len(job.increments) == 6
    assert rig.proof()["measurement"]["host_fall_bytes"] == 3 * GIB + GIB // 2 + GIB // 4


def test_a_load_that_was_not_measured_stores_no_figure_and_the_checks_still_run(tmp_path, monkeypatch, events):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)  # the flow rig's fake load opens no memory window

    async def scenario():
        await load(rig)
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    assert rig.rt._load_memory is None and "measurement" not in proof_of(rig)
    assert rig.summary()["recorded"] == "done"


# ---- proofs written before this change ---------------------------------------------------------------------
#
# tests/data/acceptance_proofs_e7b9a43.json holds proofs that the code of commit e7b9a43 wrote: the whole suite
# (version 2) run inside the load, for a GPU model, a GPU model that thinks and has tools, and a CPU model.


LEGACY = Path(__file__).parent / "data" / "acceptance_proofs_e7b9a43.json"


def install_legacy(name: str) -> dict:
    payload = json.loads(LEGACY.read_text())[name]
    stamp = time.time() - 3600
    for proof in payload["proofs"].values():
        proof["checked_at_epoch"], proof["checked_at"] = stamp, a.iso(stamp)
    path = a.get_store().path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


@pytest.mark.parametrize("name,kw", [("gpu", {}), ("gpu_thinking", {}), ("cpu", {"backend": "cpu"})])
def test_a_proof_written_by_the_code_before_this_change_is_still_honoured(tmp_path, monkeypatch, events, name, kw):
    rig = rig_with_probe(tmp_path, monkeypatch, events=events, **kw)
    payload = install_legacy(name)
    (stored,) = payload["proofs"].values()
    assert stored["key"]["suite"] == a.SUITE_VERSION == 2, "no prompt or rule changed, so no new suite version"
    on_disk = a.get_store().path().read_text()

    async def scenario():
        await load(rig)
        s = rig.summary()
        # At once, from the stored result, with every check it holds and nothing left to run.
        assert (s["status"], s["verdict"], s["from_proof"], s["recorded"]) == ("passed", "ok", True, "done")
        assert s["checks"] == a.public_checks(stored["checks"]) and len(s["checks"]) == len(stored["checks"])
        assert rig.rt.answers_verified() is True and rig.rt._recorded_job is None and rig.rt._recorded_task is None
        assert rig.models[-1].asked_kinds == [], "no check ran"
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    assert a.get_store().path().read_text() == on_disk, "the stored proof was not touched"
    report, _ = rig.check()  # an explicit test reuses it too, and its result is whole
    assert report["from_proof"] is True and report["recorded"] == "done" and rig.models[-1].asked_kinds == []


def test_a_proof_from_before_this_change_is_only_extended_never_re_decided(tmp_path, monkeypatch, events):
    """A legacy proof missing its last checks (the engine was killed) keeps its verdict and gains the rest."""
    rig = rig_with_probe(tmp_path, monkeypatch, events=events)
    payload = install_legacy("gpu_thinking")
    (key,) = payload["proofs"]
    payload["proofs"][key]["checks"] = payload["proofs"][key]["checks"][:5]  # copy ... stops
    a.get_store().path().write_text(json.dumps(payload))

    async def scenario():
        await load(rig)
        assert (rig.summary()["from_proof"], rig.summary()["recorded"]) == (True, "running")
        await rig.rt.wait_for_recorded()

    asyncio_run(scenario())
    assert rig.models[-1].asked_kinds == ["long_prompt", "reasoning", "tools", "tools"]
    proof = proof_of(rig)
    assert len(proof["checks"]) == 8 and proof["verdict"] == "ok"
    assert proof["checked_at_epoch"] == payload["proofs"][key]["checked_at_epoch"]


# ---- the pieces ------------------------------------------------------------------------------------------------


def records(*specs) -> list[dict]:
    return [{"id": i, "passed": True, "required": i in a.REQUIRED_CHECKS, **({"skipped": why} if why else {})}
            for i, why in (spec if isinstance(spec, tuple) else (spec, None) for spec in specs)]


STEPS = ["fact", "stops", "long_prompt", "reasoning", "tools"]


@pytest.mark.parametrize("stored,missing", [
    ([], STEPS),
    (["copy", "arithmetic", "repeats"], STEPS),
    (["fact", "stops"], ["long_prompt", "reasoning", "tools"]),
    (["fact", "stops", "long_prompt"], ["reasoning", "tools"]),
    (["fact", "stops", "reasoning", "tools"], []),                       # no long prompt on a CPU: not missing
    (STEPS, []),
    (["fact", ("stops", None), ("long_prompt", a.BUSY), "reasoning", "tools"], ["long_prompt"]),
    (["fact", "stops", "long_prompt", ("reasoning", a.BUSY)], ["reasoning", "tools"]),
])
def test_which_recorded_checks_a_stored_result_lacks(stored, missing):
    assert a.missing_steps(records(*stored), STEPS) == missing


def test_a_result_is_complete_when_it_holds_the_last_checks_and_none_was_cut_short():
    assert a.recorded_done(records(*a.REQUIRED_CHECKS, *STEPS)) is True
    assert a.recorded_done(records(*a.REQUIRED_CHECKS, "fact", "stops", "reasoning", "tools")) is True
    assert a.recorded_done(records(*a.REQUIRED_CHECKS)) is False
    assert a.recorded_done(records(*a.REQUIRED_CHECKS, "fact", "stops", "reasoning", ("tools", a.BUSY))) is False
    assert a.recorded_done(None) is False


def test_merged_records_are_in_suite_order_and_a_new_one_replaces_an_old_one():
    old = records("copy", "arithmetic", "repeats", ("reasoning", a.BUSY))
    new = records("fact", "stops", "reasoning", "tools")
    merged = a.merge_records(old, new)
    assert ids(merged) == ["copy", "arithmetic", "repeats", "fact", "stops", "reasoning", "tools"]
    assert "skipped" not in merged[5], "the new reasoning result replaced the one that was cut short"
    assert a.merge_records(None, new) == new and ids(a.merge_records(new, [{"id": "other"}]))[-1] == "other"


def test_resume_plan_needs_a_passing_arithmetic_result_and_says_nothing_to_do_when_complete():
    model = FakeModel()
    done = records(*a.REQUIRED_CHECKS, *STEPS)
    assert a.resume_plan(model, "quick", n_ctx=4096, on_cpu=False, records=done) is None
    assert a.resume_plan(model, "quick", n_ctx=4096, on_cpu=False, records=[]) is None
    bad = records(*a.REQUIRED_CHECKS)
    bad[1]["passed"] = False
    assert a.resume_plan(model, "quick", n_ctx=4096, on_cpu=False, records=bad) is None
    plan = a.resume_plan(model, "quick", n_ctx=4096, on_cpu=False, records=records(*a.REQUIRED_CHECKS, "fact"))
    assert plan.steps == ["stops", "long_prompt", "reasoning", "tools"]
    cpu = a.resume_plan(model, "quick", n_ctx=4096, on_cpu=True, records=records(*a.REQUIRED_CHECKS, "fact"))
    assert cpu.steps == ["stops", "reasoning", "tools"], "no long prompt in a quick check on the CPU"


def test_a_deferred_run_holds_the_required_checks_only_and_a_failed_one_has_no_plan():
    good = a.run_checks(FakeModel(), "quick", n_ctx=4096, on_cpu=False, defer_recorded=True)
    assert ids(good.api()) == REQUIRED and good.passed and good.deferred
    assert good.pending.steps == STEPS and good.output_tokens > 0
    bad = a.run_checks(FakeModel("doubled"), "quick", n_ctx=4096, on_cpu=False, defer_recorded=True)
    assert ids(bad.api()) == REQUIRED and not bad.passed and bad.deferred and bad.pending is None
    whole = a.run_checks(FakeModel(), "quick", n_ctx=4096, on_cpu=False)
    assert not whole.deferred and whole.pending is None and len(whole.outcomes) == 8


@pytest.mark.parametrize("make", [
    lambda: FakeModel("good"), lambda: FakeModel("rambles"), lambda: FakeModel("empty_think"),
    lambda: FakeModel("thinks"), lambda: Probe(events=[]),
], ids=["good", "rambles", "empty_think", "thinks", "thinking_with_tools"])
@pytest.mark.parametrize("on_cpu", [False, True])
def test_the_checks_run_later_judge_exactly_what_they_judged_inside_the_load(make, on_cpu):
    """The recorded checks read a stream when they can be stopped. The verdict on every one of them is the
    same as when the whole suite ran in the load, so no stored proof goes stale (SUITE_VERSION stays)."""
    whole = a.run_checks(make(), "quick", n_ctx=4096, on_cpu=on_cpu)
    deferred = a.run_checks(make(), "quick", n_ctx=4096, on_cpu=on_cpu, defer_recorded=True)
    later = list(deferred.outcomes)
    for step in deferred.pending.steps:
        outcome = deferred.pending.run_step(step, threading.Event())
        if outcome is not None:
            later.append(outcome)

    def shape(run_outcomes):
        return [(o.id, o.required, o.passed, o.skipped, o.finish_reason, o.excerpt) for o in run_outcomes]

    assert shape(later) == shape(whole.outcomes)


def test_a_stream_is_put_together_into_the_non_streaming_result():
    class Streams:
        def __init__(self):
            self.closed = False

        def create_chat_completion(self, **kw):
            assert kw["stream"] is True and kw["max_tokens"] == 7

            def chunks():
                try:
                    yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
                    yield {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]}
                    yield {"choices": [{"delta": {"content": "lo "}, "finish_reason": None}]}
                    yield {"choices": [{"delta": {"content": ""}, "finish_reason": None}]}
                    yield {"choices": [{"delta": {}, "finish_reason": "length"}]}
                finally:
                    self.closed = True

            return chunks()

    model = Streams()
    got = g.chat_result(model, threading.Event(), messages=[], temperature=0.0, max_tokens=7)
    assert got == {"choices": [{"message": {"content": "Hello "}, "finish_reason": "length"}],
                   "usage": {"completion_tokens": 2}}
    assert model.closed


def test_without_an_interrupt_the_call_is_the_one_the_checks_always_made():
    seen = {}

    class Plain:
        def create_chat_completion(self, **kw):
            seen.update(kw)
            return {"choices": []}

    assert g.chat_result(Plain(), None, messages=[1], temperature=0.0, max_tokens=3, tools=["t"]) == {"choices": []}
    assert seen == {"messages": [1], "temperature": 0.0, "max_tokens": 3, "tools": ["t"], "stream": False}


def test_a_reply_is_dropped_as_soon_as_the_interrupt_is_set_and_the_stream_is_closed():
    closed, produced = [], []
    interrupt = threading.Event()

    class Endless:
        def create_chat_completion(self, **kw):
            def chunks():
                try:
                    for i in range(10_000):
                        produced.append(i)
                        if i == 3:
                            interrupt.set()
                        yield {"choices": [{"delta": {"content": "w "}, "finish_reason": None}]}
                finally:
                    closed.append(True)

            return chunks()

    with pytest.raises(g.Interrupted):
        g.chat_result(Endless(), interrupt, messages=[], temperature=0.0, max_tokens=512)
    assert closed == [True] and len(produced) == 4, "it stopped at the next token"


def test_a_reply_that_was_complete_when_the_interrupt_came_is_kept():
    interrupt = threading.Event()

    class Done:
        def create_chat_completion(self, **kw):
            def chunks():
                yield {"choices": [{"delta": {"content": "391"}, "finish_reason": None}]}
                interrupt.set()
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

            return chunks()

    got = g.chat_result(Done(), interrupt, messages=[], temperature=0.0, max_tokens=5)
    assert got["choices"][0]["message"]["content"] == "391" and got["choices"][0]["finish_reason"] == "stop"


def test_a_step_that_is_interrupted_is_recorded_as_skipped_for_the_machine_being_busy_and_the_model_is_reset():
    model = Probe(events=[], slow_kinds=["reasoning"])
    run = a.run_checks(model, "quick", n_ctx=4096, on_cpu=False, defer_recorded=True)
    interrupt = threading.Event()
    interrupt.set()
    resets = model.resets
    outcome = run.pending.run_step("reasoning", interrupt)
    assert (outcome.id, outcome.passed, outcome.required, outcome.skipped) == ("reasoning", True, False, a.BUSY)
    assert outcome.api() == {"id": "reasoning", "passed": True, "required": False, "skipped": "the machine was busy"}
    assert model.resets == resets + 1
    assert run.pending.run_step("stops", interrupt).passed, "a step that asks nothing is not stopped"


def test_a_gpu_out_of_memory_in_a_recorded_check_is_raised_like_one_in_a_required_check():
    from potluck_local.services import inference_errors

    class Oom(Probe):
        def create_chat_completion(self, **kw):
            if "vault" in kw["messages"][0]["content"]:
                raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
            return super().create_chat_completion(**kw)

    model = Oom(events=[])
    run = a.run_checks(model, "quick", n_ctx=4096, on_cpu=False, defer_recorded=True)
    assert run.passed
    try:
        run.pending.run_step("long_prompt", threading.Event())
    except inference_errors.InferenceFailure as exc:
        assert exc.category == inference_errors.GPU_OUT_OF_MEMORY
    else:
        pytest.fail("the out-of-memory error was swallowed")


def test_a_gpu_that_runs_out_of_memory_in_a_recorded_check_stops_the_rest_and_leaves_the_verdict(tmp_path, monkeypatch, events, caplog):
    class Oom(Probe):
        def create_chat_completion(self, **kw):
            if "vault" in kw["messages"][0]["content"]:
                raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
            return super().create_chat_completion(**kw)

    rig = Rig(tmp_path, monkeypatch)
    rig.make_model = lambda mode: Oom(mode, events=events)

    async def scenario():
        await load(rig)
        await rig.rt.wait_for_recorded()

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        asyncio_run(scenario())
    s = rig.summary()
    assert (s["status"], s["verdict"], s["recorded"]) == ("passed", "ok", "stopped") and rig.rt.answers_verified()
    assert ids(s["checks"]) == REQUIRED + ["fact", "stops"]
    assert ids(proof_of(rig)["checks"]) == REQUIRED + ["fact", "stops"] and proof_of(rig)["verdict"] == "ok"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "answer_check_recorded_error" in warnings[0] and "category=gpu_out_of_memory" in warnings[0]
    assert "The GPU stopped" not in caplog.text and "Tried to allocate" not in caplog.text, "no model or driver text"


def test_a_recorded_check_that_throws_has_failed_that_check_and_the_others_go_on(tmp_path, monkeypatch, events, caplog):
    class Breaks(Probe):
        def create_chat_completion(self, **kw):
            if "France" in kw["messages"][0]["content"]:
                raise MemoryError("not enough memory")
            return super().create_chat_completion(**kw)

    rig = Rig(tmp_path, monkeypatch)
    rig.make_model = lambda mode: Breaks(mode, events=events)

    async def scenario():
        await load(rig)
        await rig.rt.wait_for_recorded()

    with caplog.at_level(logging.INFO, logger="potluck_local"):
        asyncio_run(scenario())
    s = rig.summary()
    assert s["status"] == "passed" and rig.rt.answers_verified() is True
    # A model that throws has failed that check, as always; the other checks went on.
    assert [c["passed"] for c in s["checks"] if c["id"] == "fact"] == [False] and s["recorded"] == "done"


def test_store_extend_adds_results_to_a_passing_entry_without_moving_its_verdict_or_time(tmp_path):
    store = a.ProofStore(tmp_path / "p.json")
    key = {"model_sha256": "abc", "suite": 2}
    store.put(key, {"verdict": "ok", "level": "quick", "duration_ms": 10, "output_tokens": 5,
                    "checks": records(*a.REQUIRED_CHECKS), "measurement": {"host_fall_bytes": 7, "samples": 1}})
    before = store.get(key)
    assert store.extend(key, records("fact", "stops"), duration_ms=30, output_tokens=4,
                        widen=lambda m: {**m, "host_fall_bytes": m["host_fall_bytes"] + 1})
    after = store.get(key)
    assert ids(after["checks"]) == ["copy", "arithmetic", "repeats", "fact", "stops"]
    assert (after["verdict"], after["checked_at_epoch"], after["key"]) == ("ok", before["checked_at_epoch"], key)
    assert (after["duration_ms"], after["output_tokens"], after["measurement"]["host_fall_bytes"]) == (40, 9, 8)
    assert store.extend({**key, "suite": 3}, records("fact")) is False, "only the entry for exactly this key"
    store.put({**key, "suite": 4}, {"verdict": "model_failed", "checks": []})
    assert store.extend({**key, "suite": 4}, records("fact")) is False, "a failed verdict is not extended"
    assert store.extend({"model_sha256": "none"}, records("fact")) is False


def test_store_extend_leaves_a_missing_measurement_missing(tmp_path):
    store = a.ProofStore(tmp_path / "p.json")
    key = {"model_sha256": "abc"}
    store.put(key, {"verdict": "ok", "checks": records(*a.REQUIRED_CHECKS)})
    assert store.extend(key, records("fact"), widen=lambda m: pytest.fail("there was nothing to widen"))
    assert "measurement" not in store.get(key)


def test_widening_the_same_measurement_again_adds_only_what_is_new():
    # A proof whose recorded checks run again on a later load is widened again. Fifty loads must not
    # turn a 3 GiB model into one that no longer fits.
    increment = [{"host_fall_bytes": GIB // 2, "rss_peak_bytes": 1, "gpu_added_bytes": GIB // 8}]
    once = memory_probe.widen(measurement(), increment)
    again = once
    for _ in range(50):
        again = memory_probe.widen(again, increment)
    assert again["host_fall_bytes"] == once["host_fall_bytes"] == 3 * GIB + GIB // 2
    assert again["available_min_bytes"] == once["available_min_bytes"] == 7 * GIB - GIB // 2
    assert again["gpu_bytes"] == once["gpu_bytes"] == 2 * GIB + GIB // 8
    more = memory_probe.widen(again, [{"host_fall_bytes": GIB, "rss_peak_bytes": 1, "gpu_added_bytes": 0}])
    assert more["host_fall_bytes"] == 4 * GIB and more["available_min_bytes"] == 6 * GIB, "a larger reading adds the difference"
    assert more["gpu_bytes"] == once["gpu_bytes"], "a smaller reading never lowers a figure"
