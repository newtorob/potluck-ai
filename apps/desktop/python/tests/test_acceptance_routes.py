"""What the answer check looks like from outside the engine: the HTTP contract,
and who is turned away when a model has not been verified."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.contributor.schemas import JobAssignedMessage
from potluck_local.routers import health, models, peer, readiness
from potluck_local.services import acceptance as a
from potluck_local.services import circle_link, contributor_service as cs
from potluck_local.services import inference_errors as ie
from potluck_local.services.settings_manager import NetworkSettings
from test_acceptance_flow import SLUG, Rig
from test_circle_gate import CHAT, auth
from test_contributor_service import FakeLoopRunner, FakeRuntime, FakeWS
from test_gpu_out_of_memory import make_runtime

FIXED_TEXT = "The serving machine has not verified this model."
GPU_WRONG = ("The GPU gave wrong answers for this model, so it is running on the CPU, which is slower. "
             "Updating the graphics driver usually fixes this.")
MODEL_FAILED = ("This model did not pass its answer check on this machine. You can still use it yourself. "
                "It is not offered to other people.")
ACCEPTANCE_KEYS = ["schema_version", "status", "model_id", "level", "checked_at", "from_proof", "ran_on",
                   "checks", "verdict", "fallback", "message", "recorded"]


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def client_for(rt, monkeypatch) -> TestClient:
    for module in (health, models):
        monkeypatch.setattr(module, "get_runtime", lambda: rt)
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(models.router, prefix="/models")
    return TestClient(app)


# ---- GET /health/inference ----------------------------------------------------------


def test_health_inference_has_an_acceptance_block_even_with_nothing_loaded(rig, monkeypatch):
    body = client_for(rig.rt, monkeypatch).get("/health/inference").json()
    assert body["acceptance"] == {
        "schema_version": 1, "status": "not_run", "model_id": None, "level": None, "checked_at": None,
        "from_proof": False, "ran_on": None, "checks": [], "verdict": None, "fallback": None, "message": None,
        "recorded": None,
    }
    assert list(body["acceptance"]) == ACCEPTANCE_KEYS


def test_health_inference_reports_a_passed_check(rig, monkeypatch):
    rig.load()
    acc = client_for(rig.rt, monkeypatch).get("/health/inference").json()["acceptance"]
    assert list(acc) == ACCEPTANCE_KEYS
    assert (acc["status"], acc["model_id"], acc["level"], acc["from_proof"], acc["verdict"], acc["fallback"],
            acc["message"]) == ("passed", SLUG, "quick", False, "ok", None, None)
    assert acc["ran_on"] == {"device_kind": "gpu", "backend": "vulkan", "device_name": "AMD Radeon RX 6600 XT",
                             "driver": rig.driver}
    assert acc["checked_at"].endswith("Z") and acc["checks"][0] == {"id": "copy", "passed": True, "required": True}


def test_health_inference_reports_the_fallback_in_the_contracts_words(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, gpu="doubled")
    rig.load()
    acc = client_for(rig.rt, monkeypatch).get("/health/inference").json()["acceptance"]
    assert acc["status"] == "passed" and acc["verdict"] == "gpu_wrong_output"
    assert acc["fallback"] == {"from": "gpu", "to": "cpu", "reason": "gpu_wrong_output"}
    assert acc["message"] == GPU_WRONG and acc["ran_on"]["device_kind"] == "cpu"


def test_health_inference_reports_a_failed_check(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    acc = client_for(rig.rt, monkeypatch).get("/health/inference").json()["acceptance"]
    assert (acc["status"], acc["verdict"], acc["fallback"], acc["message"]) == ("failed", "model_failed", None, MODEL_FAILED)


def test_health_inference_reports_skipped_when_the_owner_turned_the_check_off(rig, monkeypatch):
    rig.load(verify=False)
    acc = client_for(rig.rt, monkeypatch).get("/health/inference").json()["acceptance"]
    assert acc["status"] == "skipped" and acc["checks"] == [] and acc["message"] is None


def test_the_setting_exists_and_is_on_by_default():
    from potluck_local.services.settings_manager import PotluckSettings

    assert PotluckSettings().runtime.verify_answers is True


# ---- POST /models/{slug}/test --------------------------------------------------------------


def test_a_pass_keeps_todays_fields_and_adds_the_acceptance_object(rig, monkeypatch):
    rig.load()
    resp = client_for(rig.rt, monkeypatch).post(f"/models/{SLUG}/test", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert list(body) == ["schemaVersion", "status", "model", "durationMs", "outputTokens", "scope", "acceptance"]
    assert (body["schemaVersion"], body["status"], body["model"], body["scope"]) == (1, "passed", SLUG, "local")
    assert isinstance(body["durationMs"], int) and isinstance(body["outputTokens"], int)
    assert list(body["acceptance"]) == ACCEPTANCE_KEYS and body["acceptance"]["status"] == "passed"


def test_a_model_with_wrong_answers_now_fails_the_test(tmp_path, monkeypatch):
    """The route used to pass on any non-empty text."""
    rig = Rig(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    resp = client_for(rig.rt, monkeypatch).post(f"/models/{SLUG}/test", json={})
    assert resp.status_code == 503 and resp.json() == {"detail": MODEL_FAILED}


def test_the_test_of_a_failed_model_answers_from_its_proof_unless_forced(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, gpu="wrong", cpu="wrong")
    rig.load()
    client = client_for(rig.rt, monkeypatch)
    loads = len(rig.loads)
    resp = client.post(f"/models/{SLUG}/test", json={})
    assert resp.status_code == 503 and resp.json() == {"detail": MODEL_FAILED}
    assert len(rig.loads) == loads, "nothing was loaded or run to say so"
    assert rig.summary()["from_proof"] is True
    forced = client.post(f"/models/{SLUG}/test", json={"force": True})
    assert forced.status_code == 503 and forced.json() == {"detail": MODEL_FAILED}
    assert rig.loads[loads:] == [(0, True), (-1, False)] and rig.summary()["from_proof"] is False


def test_a_broken_gpu_found_by_the_test_moves_the_model_and_the_test_passes_with_the_message(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.load(verify=False)
    rig.models[-1].mode = "doubled"
    rig.gpu = "doubled"
    resp = client_for(rig.rt, monkeypatch).post(f"/models/{SLUG}/test", json={})
    acc = resp.json()["acceptance"]
    assert resp.status_code == 200 and acc["fallback"] is not None and acc["message"] == GPU_WRONG
    assert rig.rt._load_report["used_n_gpu_layers"] == 0


def test_the_test_reuses_a_proof_unless_it_is_forced(rig, monkeypatch):
    rig.load()
    client = client_for(rig.rt, monkeypatch)
    again = client.post(f"/models/{SLUG}/test", json={}).json()
    assert again["acceptance"]["from_proof"] is True
    forced = client.post(f"/models/{SLUG}/test", json={"force": True}).json()
    assert forced["acceptance"]["from_proof"] is False and forced["outputTokens"] > 0
    assert client.post(f"/models/{SLUG}/test", json={"force": False}).json()["acceptance"]["from_proof"] is True


def test_the_test_takes_a_level(rig, monkeypatch):
    rig.load()
    client = client_for(rig.rt, monkeypatch)
    assert client.post(f"/models/{SLUG}/test", json={"level": "full"}).json()["acceptance"]["level"] == "full"
    assert client.post(f"/models/{SLUG}/test", json={"level": "quick"}).json()["acceptance"]["level"] == "quick"
    assert client.post(f"/models/{SLUG}/test", json={}).json()["acceptance"]["level"] == "quick"
    assert client.post(f"/models/{SLUG}/test", json={"level": "thorough"}).status_code == 422
    assert client.post(f"/models/{SLUG}/test", json={"force": "yes please"}).status_code == 422


def test_the_test_names_the_model_it_checks(rig, monkeypatch):
    rig.load()
    client = client_for(rig.rt, monkeypatch)
    assert client.post("/models/another-model/test", json={}).status_code == 409
    assert client.post(f"/models/{SLUG}/test", json={"sha256": rig.rt.current_model_sha256()}).status_code == 200
    assert client.post(f"/models/{SLUG}/test", json={"sha256": "a" * 64}).status_code == 409  # a different file
    assert client.post(f"/models/{SLUG}/test", json={"sha256": "xyz"}).status_code == 422


def test_the_test_with_nothing_loaded_is_the_old_503(rig, monkeypatch):
    resp = client_for(rig.rt, monkeypatch).post(f"/models/{SLUG}/test", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Local inference test failed. Check model and runtime settings."


def test_with_the_check_off_the_test_is_the_old_probe_and_says_skipped(rig, monkeypatch, answer_check_off):
    rig.load(verify=False)
    resp = client_for(rig.rt, monkeypatch).post(f"/models/{SLUG}/test", json={})
    body = resp.json()
    assert resp.status_code == 200 and list(body)[-1] == "acceptance"
    assert body["acceptance"]["status"] == "skipped" and body["acceptance"]["model_id"] == SLUG
    assert rig.rt.answers_verified() is False


def test_the_model_load_route_passes_the_setting_to_the_runtime(tmp_path, monkeypatch):
    from potluck_local.services.model_lifecycle import ModelLifecycle
    from potluck_local.services import model_lifecycle

    seen = []

    class Runtime:
        async def load_model_async(self, path, **kwargs):
            seen.append(kwargs)
            return {"model_id": "m"}

    monkeypatch.setattr(models, "get_runtime", lambda: Runtime())
    monkeypatch.setattr(models.model_registry, "get_manifest", lambda slug: None)
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    monkeypatch.setattr(model_lifecycle, "_lifecycle", ModelLifecycle())
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "model.gguf").write_bytes(b"x")
    asyncio.run(models._load_model("m"))
    assert seen[0]["verify_answers"] is True

    from potluck_local.services.settings_manager import get_settings, save_settings

    settings = get_settings().model_copy(deep=True)
    settings.runtime.verify_answers = False
    save_settings(settings)
    asyncio.run(models._load_model("m"))
    assert seen[1]["verify_answers"] is False


# ---- the readiness self-test -------------------------------------------------------------------


def test_the_readiness_self_test_adds_the_acceptance_result_and_changes_nothing_else(rig, monkeypatch):
    rig.load()

    async def probe(runtime):
        return True, 12.5, "1-token inference completed"

    monkeypatch.setattr(readiness, "_run_inference_probe", probe)
    monkeypatch.setattr(readiness, "_infer_loaded_device", lambda runtime: "cuda")
    monkeypatch.setattr("potluck_local.services.runtime.get_runtime", lambda: rig.rt)
    app = FastAPI()
    app.include_router(readiness.router)
    body = TestClient(app).get("/readiness/gpu-test").json()
    assert {k: body[k] for k in ("ok", "device", "ran", "latency_ms", "detail")} == {
        "ok": True, "device": "cuda", "ran": "inference", "latency_ms": 12.5, "detail": "1-token inference completed"}
    assert list(body["acceptance"]) == ACCEPTANCE_KEYS and body["acceptance"]["status"] == "passed"


# ---- other machines ------------------------------------------------------------------------------


def test_the_fixed_category_and_text_are_whitelisted_on_both_sides():
    assert ie.MODEL_NOT_VERIFIED == "model_not_verified" and ie.MODEL_NOT_VERIFIED_TEXT == FIXED_TEXT
    assert ie.other_machine_error("model_not_verified") == {"category": "model_not_verified", "message": FIXED_TEXT}
    assert circle_link.fixed_category("model_not_verified") == "model_not_verified"
    assert circle_link.ERROR_TEXT["model_not_verified"] == FIXED_TEXT


@pytest.fixture()
def lender(fake_mesh):
    from test_circle_gate import Lender

    return Lender(fake_mesh)


def test_a_circle_request_is_refused_when_the_model_is_not_verified(lender):
    lender.runtime.verified = False
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"code": "model_not_verified", "message": FIXED_TEXT}}
    assert lender.runtime.calls == [], "the model was not asked"
    assert lender.admission.in_flight == 0, "the refused request took no place"


def test_a_circle_request_is_served_when_the_model_is_verified(lender):
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == 200 and lender.runtime.calls


def test_a_runtime_that_cannot_say_is_not_lent(lender):
    lender.runtime.answers_verified = lambda: (_ for _ in ()).throw(RuntimeError("no idea"))
    assert lender.client().post("/circle/chat/completions", json=CHAT, headers=auth()).status_code == 503


def test_a_load_the_check_does_not_cover_is_served_as_it_always_was(lender):
    """Managed split, pipeline and rpc loads report null: lend as before."""
    lender.runtime.verified = None
    resp = lender.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == 200 and lender.runtime.calls


def test_circle_capabilities_do_not_offer_a_model_that_would_be_refused(lender):
    caps = lambda: lender.client().get("/circle/capabilities", headers=auth()).json()  # noqa: E731
    assert caps()["available_for_inference"] is True
    lender.runtime.verified = False
    refused = caps()
    assert refused["available_for_inference"] is False
    assert refused["loaded_model"] == lender.runtime.model, "only the value changes, not the shape"
    assert list(refused) == list(lender.client().get("/circle/capabilities", headers=auth()).json())
    lender.runtime.verified = None
    assert caps()["available_for_inference"] is True  # not covered by the check: as before


def test_the_wrong_model_is_still_reported_as_the_wrong_model(lender):
    lender.runtime.verified = False
    resp = lender.client().post("/circle/chat/completions", json={**CHAT, "model": "other"}, headers=auth())
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "model_mismatch"


def job(runtime) -> JobAssignedMessage:
    return JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(), model_slug=runtime.model_id, prompt="hi",
                              sampling={"max_tokens": 8})


def serve(runtime, *, network=None):
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = network or NetworkSettings(contributor_mode=True)
    ws = FakeWS()
    asyncio.run(service._serve_job(ws, job(runtime)))
    return service, ws


def test_a_relayed_job_is_declined_with_the_fixed_text_when_the_model_is_not_verified():
    runtime = FakeRuntime()
    runtime.verified = False
    _, ws = serve(runtime)
    assert [f["type"] for f in ws.sent] == ["job_error"]
    assert ws.sent[0]["error_category"] == "declined" and ws.sent[0]["error_message"] == FIXED_TEXT
    assert runtime.calls == []


@pytest.mark.parametrize("verified", [True, None])
def test_a_relayed_job_is_served_when_the_model_is_verified_or_not_covered(verified):
    runtime = FakeRuntime()
    runtime.verified = verified
    _, ws = serve(runtime)
    assert [f["type"] for f in ws.sent] == ["job_token", "job_token", "job_complete"]


def test_a_relayed_job_for_another_model_is_still_declined_for_that_reason():
    runtime = FakeRuntime()
    runtime.verified = False
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True)
    ws = FakeWS()
    other = JobAssignedMessage(type="job_assigned", job_id=uuid.uuid4(), model_slug="other", prompt="hi")
    asyncio.run(service._serve_job(ws, other))
    assert "other" in ws.sent[0]["error_message"]


def advertised(runtime) -> list[str]:
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True)
    return [c.model_slug for c in asyncio.run(service._capabilities())]


@pytest.mark.parametrize("verified,offered", [(True, True), (None, True), (False, False)])
def test_the_coordinator_is_offered_only_what_will_be_served(verified, offered):
    runtime = FakeRuntime()
    runtime.verified = verified
    assert advertised(runtime) == ([runtime.model_id] if offered else [])


def test_an_unverified_model_is_offered_once_it_passes_through_the_reattestation_path(monkeypatch):
    """Capabilities travel only in the attestation. The watch that notices a model
    swap notices this too, because the provider now follows the check."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from potluck_local.contributor import client as contributor
    from test_contributor_loop import FakeCoordinator, _hooks, _settings

    monkeypatch.setattr(contributor, "CAPABILITY_CHECK_INTERVAL_S", 0.05)
    monkeypatch.setattr(contributor, "RECONNECT_BACKOFF_INITIAL_S", 0.05)
    runtime = FakeRuntime()
    runtime.verified = False
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True)

    async def scenario():
        async with FakeCoordinator() as coord:
            hooks = _hooks(capabilities_provider=service._capabilities)
            stop = asyncio.Event()
            task = asyncio.create_task(contributor.run_contributor_loop(
                _settings(coord.url), Ed25519PrivateKey.generate(), hooks, stop))
            await coord.wait_connected()
            first = [c["model_slug"] for c in coord.attestations[0]["capabilities"]]
            runtime.verified = True  # the check finished and passed
            deadline = asyncio.get_event_loop().time() + 3
            while len(coord.attestations) < 2 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.02)
            await coord.wait_connected()
            runtime.verified = False  # and a later check that does not
            deadline = asyncio.get_event_loop().time() + 3
            while len(coord.attestations) < 3 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.02)
            await coord.wait_connected()
            stop.set()
            await asyncio.wait_for(task, 5)
            return first, [[c["model_slug"] for c in a["capabilities"]] for a in coord.attestations]

    first, attested = asyncio.run(scenario())
    assert first == [] and attested == [[], [runtime.model_id], []]


def status_of(runtime) -> dict:
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True)
    service._status.state = "connected"
    return service.status()


def test_the_contributor_status_says_why_lending_is_paused():
    runtime = FakeRuntime()
    assert (status_of(runtime)["pause_reason"], status_of(runtime)["accepting_jobs"]) == (None, True)
    runtime.verified = False
    paused = status_of(runtime)
    assert (paused["pause_reason"], paused["accepting_jobs"]) == ("model_not_verified", False)
    runtime.verified = None  # a load the check does not cover is not paused
    assert (status_of(runtime)["pause_reason"], status_of(runtime)["accepting_jobs"]) == (None, True)
    runtime.verified = False
    runtime.model_id = None  # nothing loaded: nothing to be unverified
    assert status_of(runtime)["pause_reason"] is None


def test_an_owner_policy_reason_comes_before_the_verification_reason():
    runtime = FakeRuntime()
    runtime.verified = False
    service = cs.ContributorService(runtime_getter=lambda: runtime, loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True, max_concurrent_jobs=0)
    assert service.status()["pause_reason"] == "job_limit_zero"


def test_a_status_never_fails_because_the_runtime_cannot_say():
    class Odd:
        def is_loaded(self):
            return True

    service = cs.ContributorService(runtime_getter=lambda: Odd(), loop_runner=FakeLoopRunner())
    service._network = NetworkSettings(contributor_mode=True)
    assert service.status()["pause_reason"] is None


# ---- household peers --------------------------------------------------------------------------------


def peer_client(rt, monkeypatch) -> TestClient:
    monkeypatch.setattr(peer, "get_runtime", lambda: rt)
    monkeypatch.setattr(peer, "get_settings", lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))

    async def hardware():
        return {}

    async def record(*args, **kwargs):
        pass

    monkeypatch.setattr(peer, "_cached_hardware", hardware)
    monkeypatch.setattr(peer, "_record_served", record)
    monkeypatch.setattr("potluck_local.services.link_detect.detect_link_type", lambda: "wired")
    app = FastAPI()
    app.include_router(peer.router, prefix="/peer")
    return TestClient(app)


def test_household_peers_are_served_whether_or_not_the_model_is_verified(monkeypatch):
    rt = make_runtime(None)
    rt._acceptance = a.summary(status="failed", model_id="test", verdict="model_failed", message=MODEL_FAILED)
    assert rt.answers_verified() is False
    resp = peer_client(rt, monkeypatch).post("/peer/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}], "model": "test", "stream": True})
    assert resp.status_code == 200 and "hello" in resp.text and "error" not in resp.text


@pytest.mark.parametrize("state,expected", [
    (a.summary(status="passed", model_id="test"), True),
    (a.summary(status="failed", model_id="test"), False),
    (a.summary(status="skipped", model_id="test"), False),
    (None, False),
])
def test_capabilities_say_whether_the_answers_are_verified(monkeypatch, state, expected):
    rt = make_runtime(None)
    rt._acceptance = state
    body = peer_client(rt, monkeypatch).get("/peer/capabilities").json()
    assert body["answers_verified"] is expected and body["available_for_inference"] is True


def test_capabilities_say_null_for_a_load_the_check_does_not_cover(monkeypatch):
    rt = make_runtime(None)
    rt._loaded_rpc_servers = ("100.64.0.6:50052",)
    body = peer_client(rt, monkeypatch).get("/peer/capabilities").json()
    assert body["answers_verified"] is None and body["available_for_inference"] is True


def test_capabilities_say_null_when_nothing_is_loaded(monkeypatch):
    from potluck_local.services.runtime import LLMRuntime

    body = peer_client(LLMRuntime(), monkeypatch).get("/peer/capabilities").json()
    assert body["answers_verified"] is None and body["loaded_model"] is None
    assert body["protocol_version"] == 4 and "hostname" in body  # the rest of the payload is as it was
