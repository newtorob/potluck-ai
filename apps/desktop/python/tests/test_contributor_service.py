"""The in-process contributor service (M2.5): settings drive the loop,
jobs are served through the runtime, status reflects the loop's events."""
from __future__ import annotations

import asyncio
import functools
import json
import uuid

import pytest

from fastapi.testclient import TestClient

from potluck_local.contributor.schemas import JobAssignedMessage
from potluck_local.services import contributor_service as cs
from potluck_local.services.settings_manager import NetworkSettings, PotluckSettings

_LOOPBACK = ("127.0.0.1", 54321)


@pytest.fixture(autouse=True)
def _open_pool_on(monkeypatch):
    """These tests exercise the open-pool contributor itself."""
    monkeypatch.setenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", "1")


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeLoopRunner:
    """Stands in for run_contributor_loop: records what it was started with
    and returns when its stop event is set."""

    def __init__(self):
        self.starts: list[dict] = []
        self.stops = 0

    async def __call__(self, settings, priv, hooks, stop):
        self.starts.append({"settings": settings, "hooks": hooks, "stop": stop})
        await stop.wait()
        self.stops += 1
        hooks.on_state("stopped")


class FakeRuntime:
    def __init__(self, model_id="qwen3-4b-instruct-2507-q4", chunks=None, bench=None):
        self.model_id = model_id
        self.chunks = chunks if chunks is not None else [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
        ]
        self.bench = bench or {}
        self.calls: list[dict] = []
        self.verified = True  # the answer check (services/acceptance.py)

    def answers_verified(self):
        return self.verified if self.model_id is not None else None

    def is_loaded(self):
        return self.model_id is not None

    def current_model_id(self):
        return self.model_id

    def benchmarks(self):
        return self.bench

    async def stream_chat_raw(self, messages, *, temperature, max_tokens, stop):
        self.calls.append({"messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, "stop": stop})
        for c in self.chunks:
            await asyncio.sleep(0)
            yield c


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@run_async
async def test_admission_rechecks_live_policy_before_serving():
    rt = FakeRuntime()
    svc = cs.ContributorService(runtime_getter=lambda: rt)
    job = JobAssignedMessage(type='job_assigned', job_id=uuid.uuid4(), model_slug=rt.model_id,
                             prompt='private', sampling={'max_tokens': 100})
    for network in [NetworkSettings(contributor_mode=False),
                    NetworkSettings(contributor_mode=True, max_concurrent_jobs=0),
                    NetworkSettings(contributor_mode=True, contributor_max_tokens=50),
                    NetworkSettings(contributor_mode=True, contributor_schedule={'invalid': True})]:
        svc._network = network
        ws = FakeWS()
        await svc._serve_job(ws, job)
        assert ws.sent[0]['type'] == 'job_error' and ws.sent[0]['error_category'] == 'declined'
        if network.contributor_max_tokens is None: assert await svc._capabilities() == []
    assert rt.calls == []


def _settings(on=True, url="https://coord.example", cap=1, audience=None) -> PotluckSettings:
    return PotluckSettings(network=NetworkSettings(
        contributor_mode=on, coordinator_url=url, max_concurrent_jobs=cap,
        contributor_audience=audience))


def _service(runner=None, runtime=None, monkeypatch=None, tmp_path=None):
    if monkeypatch is not None and tmp_path is not None:
        monkeypatch.setenv("POTLUCK_KEY_DIR", str(tmp_path / "keys"))
        monkeypatch.delenv("POTLUCK_BINARY_HASH", raising=False)
        monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
        monkeypatch.delenv("POTLUCK_COORDINATOR_HTTP_URL", raising=False)
        monkeypatch.delenv("POTLUCK_COORDINATOR_URL", raising=False)
        from potluck_local.contributor.config import Settings as ContributorSettings

        # The dev checkout's .env (bench-fleet values) must not leak into
        # these tests: build the base contributor settings without it.
        monkeypatch.setattr(
            cs, "get_contributor_settings",
            lambda: ContributorSettings(_env_file=None, key_dir=tmp_path / "keys"),
        )

    async def hw():
        return {"gpu_name": "FakeGPU", "gpu_vram_gb": 12.0, "recommended_tier": "medium"}

    return cs.ContributorService(
        runtime_getter=lambda: runtime or FakeRuntime(),
        loop_runner=runner or FakeLoopRunner(),
        hardware_getter=hw,
    )


def _patch_base(monkeypatch, base):
    from potluck_local.services import pool_routing

    monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: base)


# ---- URL + reject helpers ---------------------------------------------------


def test_ws_url_from_http_base():
    assert cs.ws_url_from_http_base("https://coordinator.trypotluck.ai") == \
        "wss://coordinator.trypotluck.ai/peers/connect"
    assert cs.ws_url_from_http_base("http://192.168.4.78:8400/") == \
        "ws://192.168.4.78:8400/peers/connect"
    assert cs.ws_url_from_http_base("ws://127.0.0.1:1/peers/connect") == "ws://127.0.0.1:1/peers/connect"


def test_session_token_never_travels_unencrypted_to_a_public_host():
    import pytest

    for safe in ("http://localhost:8400", "http://127.0.0.1:8400", "http://[::1]:8400",
                 "http://10.0.0.5:8400", "http://172.16.1.2:8400", "http://100.101.102.103:8400"):
        assert cs.ws_url_from_http_base(safe).startswith("ws://"), safe
    for unsafe in ("http://coordinator.example.com", "ws://203.0.113.5:8400",
                   "http://coordinator.trypotluck.ai", "ftp://coordinator.trypotluck.ai"):
        with pytest.raises(ValueError):
            cs.ws_url_from_http_base(unsafe)


def test_reject_category_mapping():
    assert cs.reject_category("peer_public_key is not registered for any member — x") == \
        "device_key_not_registered"
    assert cs.reject_category("binary_hash abc…: not in contributor_releases") == \
        "release_not_registered"
    assert cs.reject_category("unauthorized") == "unauthorized"
    assert cs.reject_category("peer_public_key is registered to a different member") == \
        "key_bound_to_other_member"
    assert cs.reject_category("nonce_signature: does not verify") == "rejected"


# ---- settings drive the loop -----------------------------------------------


@run_async
async def test_toggle_on_starts_loop_with_ws_url_cap_and_hardware(monkeypatch, tmp_path):
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "http://192.168.4.78:8400")

    await svc.apply_settings(_settings(on=True, cap=2))
    assert svc.is_running
    await asyncio.sleep(0)
    start = runner.starts[0]
    assert start["settings"].coordinator_url == "ws://192.168.4.78:8400/peers/connect"
    assert start["settings"].max_concurrent_jobs == 2
    assert start["settings"].binary_hash == "unreleased-dev"
    assert start["hooks"].max_concurrent_jobs == 2
    summary = start["hooks"].hardware_summary_provider()
    assert summary["gpu_name"] == "FakeGPU" and "system" in summary
    st = svc.status()
    assert st["state"] == "connecting" and st["enabled"] and st["running"]
    assert st["coordinator_host"] == "192.168.4.78:8400"
    assert st["binary_hash_source"] == "dev"

    await svc.apply_settings(_settings(on=False))
    assert not svc.is_running
    assert runner.stops == 1
    assert svc.status()["state"] == "stopped"


@run_async
async def test_same_settings_do_not_restart_but_url_change_does(monkeypatch, tmp_path):
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "http://10.0.0.1:8400")
    await svc.apply_settings(_settings(on=True))
    await asyncio.sleep(0)
    await svc.apply_settings(_settings(on=True))
    await asyncio.sleep(0)
    assert len(runner.starts) == 1
    _patch_base(monkeypatch, "http://10.0.0.2:8400")
    await svc.apply_settings(_settings(on=True))
    await asyncio.sleep(0)
    assert len(runner.starts) == 2 and runner.stops == 1
    assert runner.starts[1]["settings"].coordinator_url == "ws://10.0.0.2:8400/peers/connect"
    await svc.stop()


@run_async
async def test_missing_coordinator_url_is_an_error_state_not_a_start(monkeypatch, tmp_path):
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, None)
    await svc.apply_settings(_settings(on=True, url=""))
    assert not svc.is_running and runner.starts == []
    st = svc.status()
    assert st["state"] == "error" and st["last_error"] == "coordinator_not_configured"


@run_async
async def test_releases_without_the_open_pool_never_start_the_contributor(monkeypatch, tmp_path):
    monkeypatch.delenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", raising=False)
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "https://coord.example")
    await svc.apply_settings(_settings(on=True))
    assert not svc.is_running and runner.starts == []
    st = svc.status()
    assert st["state"] == "disabled" and st["enabled"] is True


# ---- who the machine is shared with -----------------------------------------


@pytest.mark.parametrize("open_pool,audience,expected", [
    (False, None, None),                      # a release, nothing chosen: own machines only
    (False, "mine", None),
    (False, "circles", ["circle"]),           # circles need no open pool
    (False, "everyone", ["circle"]),          # the pool is not this build's to offer
    (True, None, ["circle", "pool"]),         # what open-pool builds did before the choice
    (True, "circles", ["circle"]),
    (True, "everyone", ["circle", "pool"]),
    (True, "mine", None),
])
def test_sharing_audiences(monkeypatch, open_pool, audience, expected):
    if open_pool:
        monkeypatch.setenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", "1")
    else:
        monkeypatch.delenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", raising=False)
    assert cs.sharing_audiences(NetworkSettings(contributor_audience=audience)) == expected


@run_async
async def test_a_release_can_share_with_circles_and_never_names_the_pool(monkeypatch, tmp_path):
    monkeypatch.delenv("POTLUCK_OPEN_POOL_CONTRIBUTOR", raising=False)
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "https://coord.example")
    await svc.apply_settings(_settings(on=True, audience="circles"))
    await asyncio.sleep(0)
    assert svc.is_running and len(runner.starts) == 1
    assert runner.starts[0]["hooks"].audiences == ["circle"]
    assert svc.status()["audiences"] == ["circle"]
    await svc.stop()


@run_async
async def test_changing_who_the_machine_serves_reconnects(monkeypatch, tmp_path):
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "https://coord.example")
    await svc.apply_settings(_settings(on=True, audience="everyone"))
    await asyncio.sleep(0)
    await svc.apply_settings(_settings(on=True, audience="circles"))
    await asyncio.sleep(0)
    assert [st["hooks"].audiences for st in runner.starts] == [["circle", "pool"], ["circle"]]
    await svc.apply_settings(_settings(on=True, audience="mine"))
    assert not svc.is_running and svc.status()["state"] == "disabled"


@run_async
async def test_engine_registers_its_machine_key_once_when_the_coordinator_does_not_know_it(monkeypatch, tmp_path):
    """A machine run from the CLI has no app to register its key, so the
    engine does it when it is turned away for that reason."""
    import httpx

    from potluck_local.services import pool_routing

    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(201, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: "https://coord.example")
    svc = _service(monkeypatch=monkeypatch, tmp_path=tmp_path)
    reason = "peer_public_key is not registered for any member"

    svc._on_state("rejected", reason=reason)          # not signed in: nothing to register for
    await asyncio.sleep(0.05)
    assert posts == []

    svc.set_member_session("sess-abc")
    svc._on_state("rejected", reason=reason)
    svc._on_state("rejected", reason=reason)          # the retry must not register again
    await asyncio.sleep(0.1)
    assert len(posts) == 1
    assert posts[0].url.path == "/me/device-keys"
    assert posts[0].headers["authorization"] == "Bearer sess-abc"
    body = json.loads(posts[0].content)
    assert body["public_key"] == svc_public_key(tmp_path)

    svc._on_state("rejected", reason="unauthorized")  # other refusals never register a key
    await asyncio.sleep(0.05)
    assert len(posts) == 1

    svc.set_member_session("sess-other")              # a new sign-in may need it again
    svc._on_state("rejected", reason=reason)
    await asyncio.sleep(0.1)
    assert len(posts) == 2


def svc_public_key(tmp_path) -> str:
    from potluck_local.contributor.keys import ensure_keypair, public_key_b64

    return public_key_b64(ensure_keypair(tmp_path / "keys").public_key())


def test_work_served_by_a_circle_only_machine_is_counted_as_circle_work():
    svc = cs.ContributorService()
    assert svc._served_scope() == "pool", "unknown audience keeps the old label"
    svc._status.audiences = ["circle", "pool"]
    assert svc._served_scope() == "pool"
    svc._status.audiences = ["circle"]
    assert svc._served_scope() == "circle"


def test_an_outdated_coordinator_has_its_own_reject_category():
    assert cs.reject_category("this coordinator cannot limit who the machine serves") == "coordinator_outdated"


@run_async
async def test_insecure_public_coordinator_is_an_error_state_not_a_start(monkeypatch, tmp_path):
    runner = FakeLoopRunner()
    svc = _service(runner, monkeypatch=monkeypatch, tmp_path=tmp_path)
    _patch_base(monkeypatch, "http://coordinator.example.com")
    await svc.apply_settings(_settings(on=True, url="http://coordinator.example.com"))
    assert not svc.is_running and runner.starts == []
    st = svc.status()
    assert st["state"] == "error" and st["last_error"] == "coordinator_insecure"


@run_async
async def test_headers_prefer_member_session_then_shared_token(monkeypatch, tmp_path):
    svc = _service(monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert svc._headers() is None
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "shared")
    assert svc._headers() == [("X-Potluck-Coordinator-Token", "shared")]
    svc.set_member_session("sess-abc")
    assert svc._headers() == [("Authorization", "Bearer sess-abc")]
    assert svc.has_member_session()
    svc.set_member_session(None)
    assert not svc.has_member_session()


# ---- capabilities + jobs through the runtime -------------------------------


@run_async
async def test_capabilities_come_from_loaded_model_and_benchmarks():
    rt = FakeRuntime(bench={"qwen3-4b-instruct-2507-q4": {
        "tokens_per_sec": 33.3, "prompt_tokens_per_sec": 410.0}})
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    caps = await svc._capabilities()
    assert len(caps) == 1
    assert caps[0].model_slug == "qwen3-4b-instruct-2507-q4"
    assert caps[0].quantization == "q4"
    assert caps[0].tokens_per_second == 33.3
    assert caps[0].prompt_tokens_per_second == 410.0
    rt.model_id = None
    assert await svc._capabilities() == []


@run_async
async def test_job_streams_through_runtime_and_counts(monkeypatch):
    rt = FakeRuntime()
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    recorded = []

    async def fake_record(**kw):
        recorded.append(kw)

    import potluck_local.services.usage_store as us

    monkeypatch.setattr(us, "record_usage_event", fake_record)
    ws = FakeWS()
    job = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                             model_slug="qwen3-4b-instruct-2507-q4", prompt="hi",
                             sampling={"max_tokens": 8, "temperature": 0.2,
                                       "stop_sequences": ["</s>"]})
    await svc._serve_job(ws, job)
    kinds = [f["type"] for f in ws.sent]
    assert kinds == ["job_token", "job_token", "job_complete"]
    assert [f["token_text"] for f in ws.sent[:2]] == ["Hel", "lo"]
    assert ws.sent[2]["output_tokens"] == 2 and ws.sent[2]["finish_reason"] == "stop"
    assert rt.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert rt.calls[0]["temperature"] == 0.2 and rt.calls[0]["stop"] == ["</s>"]
    assert svc.status()["jobs_served"] == 1
    assert recorded and recorded[0]["direction"] == "served" and recorded[0]["output_tokens"] == 2


@run_async
async def test_job_uses_messages_when_present_and_reports_length():
    rt = FakeRuntime(chunks=[{"choices": [{"delta": {"content": "a"}}]}] * 3)
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    ws = FakeWS()
    job = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                             model_slug="qwen3-4b-instruct-2507-q4", prompt="last",
                             messages=[{"role": "system", "content": "s"},
                                       {"role": "user", "content": "last"}],
                             sampling={"max_tokens": 3})
    await svc._serve_job(ws, job)
    assert rt.calls[0]["messages"][0] == {"role": "system", "content": "s"}
    assert ws.sent[-1]["finish_reason"] == "length"


@run_async
async def test_job_declined_on_model_mismatch_and_no_model():
    rt = FakeRuntime()
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    ws = FakeWS()
    job = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                             model_slug="other-model", prompt="hi")
    await svc._serve_job(ws, job)
    assert ws.sent[0]["type"] == "job_error" and ws.sent[0]["error_category"] == "declined"
    assert "other-model" in ws.sent[0]["error_message"]
    rt.model_id = None
    ws = FakeWS()
    await svc._serve_job(ws, job)
    assert ws.sent[0]["error_category"] == "model_not_loaded"
    assert svc.status()["jobs_served"] == 0


@run_async
async def test_runtime_error_becomes_internal_job_error_without_detail_leak():
    rt = FakeRuntime(chunks=[{"choices": [{"delta": {"content": "x"}}]},
                             {"__error__": "CUDA out of memory at 0xdeadbeef prompt=secret"}])
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    ws = FakeWS()
    job = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                             model_slug="qwen3-4b-instruct-2507-q4", prompt="hi")
    await svc._serve_job(ws, job)
    err = ws.sent[-1]
    assert err["type"] == "job_error" and err["error_category"] == "internal"
    assert err["error_message"] == "RuntimeError"


# ---- status from loop events -----------------------------------------------


def test_status_tracks_loop_events():
    svc = cs.ContributorService(runtime_getter=FakeRuntime, loop_runner=FakeLoopRunner())
    svc._status = cs.ContributorStatus(enabled=True, state="connecting")
    svc._on_state("connected", peer_id="p1", session_id="s1", heartbeat_interval_s=30)
    st = svc.status()
    assert st["state"] == "connected" and st["peer_id"] == "p1" and st["connected_since"]
    svc._on_state("heartbeat", current_jobs=0)
    svc._on_state("job_started", job_id="j")
    assert svc.status()["jobs_in_flight"] == 1
    svc._on_state("job_finished", job_id="j", ok=True)
    assert svc.status()["jobs_in_flight"] == 0
    assert svc.status()["heartbeats_sent"] == 1
    svc._on_state("disconnected", error="ConnectionClosedError")
    st = svc.status()
    assert st["state"] == "connecting" and st["reconnects"] == 1 and st["peer_id"] is None
    svc._on_state("rejected", reason="peer_public_key is not registered for any member")
    st = svc.status()
    assert st["state"] == "rejected" and st["last_error"] == "device_key_not_registered"
    svc._on_state("connecting")
    assert svc.status()["state"] == "rejected", "a retry after a reject keeps the reject visible"
    svc._on_state("connected", peer_id="p2", session_id="s2", heartbeat_interval_s=30)
    assert svc.status()["state"] == "connected" and svc.status()["last_error"] is None
    svc._on_state("stopped")
    assert svc.status()["state"] == "stopped"


# ---- HTTP surface -----------------------------------------------------------


def test_status_and_session_endpoints(monkeypatch, tmp_path, caplog):
    from potluck_local.app import create_app

    # App boot hands a stored sign-in to the contributor; keep the
    # developer's real one out of the test.
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    fake = cs.ContributorService(runtime_getter=FakeRuntime, loop_runner=FakeLoopRunner())
    cs.set_contributor_service_for_tests(fake)
    try:
        app = create_app()
        with TestClient(app, client=_LOOPBACK) as client:
            body = client.get("/contributor/status").json()
            assert body["state"] == "stopped" and body["has_member_session"] is False
            assert set(body) >= {"enabled", "state", "peer_id", "heartbeats_sent",
                                 "reconnects", "jobs_served", "jobs_in_flight",
                                 "last_error", "coordinator_host", "running"}
            r = client.put("/contributor/session", json={"token": "very-secret-session"})
            assert r.status_code == 204 and r.text == ""
            assert client.get("/contributor/status").json()["has_member_session"] is True
            assert "very-secret-session" not in caplog.text
            assert client.put("/contributor/session", json={"token": ""}).status_code == 422
            assert client.delete("/contributor/session").status_code == 204
            assert client.get("/contributor/status").json()["has_member_session"] is False
    finally:
        cs.set_contributor_service_for_tests(None)


def test_settings_patch_reconciles_contributor(monkeypatch, tmp_path):
    from potluck_local.app import create_app
    from potluck_local.services import settings_manager

    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_manager, "_default_config_path",
                        lambda: tmp_path / "config.json")
    settings_manager.get_settings.cache_clear() if hasattr(settings_manager.get_settings, "cache_clear") else None

    applied: list[bool] = []

    class Spy(cs.ContributorService):
        async def apply_settings(self, settings):
            applied.append(settings.network.contributor_mode)

    cs.set_contributor_service_for_tests(Spy(runtime_getter=FakeRuntime, loop_runner=FakeLoopRunner()))
    try:
        app = create_app()
        with TestClient(app, client=_LOOPBACK) as client:
            startup_calls = len(applied)
            r = client.patch("/settings", json={"network": {"contributor_mode": True,
                                                            "max_concurrent_jobs": 2}})
            assert r.status_code == 200
            assert r.json()["network"]["max_concurrent_jobs"] == 2
            assert applied[startup_calls:] == [True]
            client.patch("/settings", json={"network": {"contributor_mode": False}})
            assert applied[-1] is False
    finally:
        cs.set_contributor_service_for_tests(None)


@run_async
async def test_gpu_out_of_memory_becomes_the_coordinators_oom_with_fixed_text():
    rt = FakeRuntime(chunks=[{"choices": [{"delta": {"content": "x"}}]},
                             {"__error__": "[WinError -529697949] Windows Error 0xe06d7363",
                              "__category__": "gpu_out_of_memory"}])
    svc = cs.ContributorService(runtime_getter=lambda: rt, loop_runner=FakeLoopRunner())
    ws = FakeWS()
    job = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(),
                             model_slug="qwen3-4b-instruct-2507-q4", prompt="hi")
    await svc._serve_job(ws, job)
    err = ws.sent[-1]
    assert err["type"] == "job_error" and err["error_category"] == "oom"
    assert err["error_message"] == "The serving machine's GPU most likely ran out of memory."
    assert "WinError" not in str(ws.sent) and svc.status()["jobs_served"] == 0
