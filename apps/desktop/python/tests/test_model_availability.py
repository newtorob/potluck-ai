import asyncio
from types import SimpleNamespace

import httpx
import pytest

from potluck_local.services import model_availability as ma


@pytest.fixture
def inventory(monkeypatch, tmp_path):
    monkeypatch.setenv("POTLUCK_DATA_DIR", str(tmp_path))
    roster = [{"mesh_ip": "100.64.0.2", "node_name": "desktop"},
              {"mesh_ip": "100.64.0.3", "node_name": "laptop"}]
    responses = {}
    calls = []
    async def peers():
        return list(roster)
    async def handler(request):
        calls.append(request)
        result = responses.get((request.url.host, request.url.path))
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json=result) if result is not None else httpx.Response(503)
    client = httpx.AsyncClient
    monkeypatch.setattr(ma, "household_peers", peers)
    monkeypatch.setattr(ma, "get_runtime", lambda: SimpleNamespace(is_loaded=lambda: False))
    monkeypatch.setattr(ma, "get_split_manager", lambda: SimpleNamespace(status=lambda: {"engine": {"available": True}}))
    monkeypatch.setattr(ma.httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    return roster, responses, calls


def serving(sha="a" * 64, busy=False, **extra):
    return {"loaded_model": "coder", "available_for_inference": True, "model_lock_version": 1,
            "loaded_model_detail": {"sha256": sha}, "inference_load": {"busy": busy}, **extra}


def test_exact_versions_grouped_capacity_counts_only_idle_runtimes(inventory):
    _, responses, calls = inventory
    responses[("100.64.0.2", "/peer/capabilities")] = serving()
    responses[("100.64.0.3", "/peer/capabilities")] = serving(busy=True)
    result = asyncio.run(ma.model_availability())
    assert result["additionalModels"] == 1
    assert len(result["models"]) == 1
    assert len(result["models"][0]["machines"]) == 2
    assert result["models"][0]["idleMachineCount"] == 1
    assert all(request.method == "GET" for request in calls)
    responses[("100.64.0.3", "/peer/capabilities")] = serving(sha="b" * 64)
    result = asyncio.run(ma.model_availability())
    assert len(result["models"]) == 2  # A matching ID cannot merge different bytes.
    assert result["additionalModels"] == 1


def test_missing_hash_not_invented_and_sharing_off_not_advertised(inventory):
    _, responses, _ = inventory
    responses[("100.64.0.2", "/peer/capabilities")] = serving(model_lock_version=0)
    responses[("100.64.0.3", "/peer/capabilities")] = serving(available_for_inference=False)
    result = asyncio.run(ma.model_availability())
    assert len(result["models"]) == 1 and result["models"][0]["sha256"] is None


def test_worker_requires_opt_in_matching_protocol_and_engine(inventory):
    _, responses, _ = inventory
    status = {"protocol": ma.PIN["protocol"], "engine": {"available": True, "commit": ma.PIN["commit"], "backend": "cpu"},
              "available": True, "freeMemoryBytes": 8 * 1024**3}
    responses[("100.64.0.2", "/peer/split/status")] = status
    responses[("100.64.0.3", "/peer/split/status")] = {**status, "protocol": 99}
    result = asyncio.run(ma.model_availability())
    assert result["splitWorkerCount"] == 1
    assert result["additionalModels"] == 0  # Free memory cannot prove a model works.
    status["available"] = False
    assert asyncio.run(ma.model_availability())["splitWorkerCount"] == 0


def test_disconnect_and_malformed_response_fail_closed(inventory):
    roster, responses, _ = inventory
    responses[("100.64.0.2", "/peer/capabilities")] = serving()
    assert asyncio.run(ma.model_availability())["additionalModels"] == 1
    responses[("100.64.0.2", "/peer/capabilities")] = httpx.ConnectError("offline")
    responses[("100.64.0.3", "/peer/capabilities")] = ["not capabilities"]
    result = asyncio.run(ma.model_availability())
    assert result["models"] == []
    assert not any(m["responding"] for m in result["machines"])
    roster.clear()
    assert asyncio.run(ma.model_availability())["machines"] == []


def test_removed_during_poll_is_not_advertised(inventory, monkeypatch):
    roster, responses, _ = inventory
    responses[("100.64.0.2", "/peer/capabilities")] = serving()
    calls = 0
    async def peers():
        nonlocal calls
        calls += 1
        return roster if calls == 1 else []
    monkeypatch.setattr(ma, "household_peers", peers)
    result = asyncio.run(ma.model_availability())
    assert result["models"] == result["machines"] == []


def test_local_model_is_not_counted_as_new_choice(inventory, monkeypatch):
    _, responses, _ = inventory
    monkeypatch.setattr(ma, "get_runtime", lambda: SimpleNamespace(is_loaded=lambda: True,
        current_model_id=lambda: "coder", current_model_sha256=lambda: "a" * 64))
    responses[("100.64.0.2", "/peer/capabilities")] = serving()
    result = asyncio.run(ma.model_availability())
    assert result["models"][0]["local"] is True
    assert result["additionalModels"] == 0
    assert result["models"][0]["idleMachineCount"] == 1


def test_bounds_and_owned_addresses_only(inventory):
    roster, responses, calls = inventory
    roster.extend([{"mesh_ip": "127.0.0.1", "node_name": "local"},
                   {"mesh_ip": "https://example.com", "node_name": "bad"}])
    roster.extend({"mesh_ip": f"100.64.1.{n}", "node_name": str(n)} for n in range(1, 40))
    result = asyncio.run(ma.model_availability())
    assert result["truncated"] is True
    assert len(result["machines"]) == ma.MAX_PEERS
    assert all(request.url.host.startswith("100.64.") for request in calls)


def test_imported_artifact_can_be_selected_without_catalog_entry(inventory, tmp_path):
    model = tmp_path / "models" / "imported-32b"
    model.mkdir(parents=True)
    (model / "model.gguf").write_bytes(b"GGUF-test")
    outside = tmp_path / "other-model"
    outside.mkdir()
    (outside / "model.gguf").write_bytes(b"GGUF-test")
    (tmp_path / "models" / "escaping-dir").symlink_to(outside, target_is_directory=True)
    result = asyncio.run(ma.model_availability())
    assert result["installedModels"] == [{"id": "imported-32b", "sizeBytes": 9}]
