"""Tests for GET /readiness/gpu-test.

No model downloads, no fleet, no network — just the FastAPI TestClient.
On this Mac (M3 Pro, Apple Silicon) we expect:
  - device: "mps" (or "cpu" if MPS unavailable in CI)
  - ran:    "compute"  (no model loaded)
  - ok:     True when torch is present and the matmul runs cleanly
"""

from __future__ import annotations

import importlib.util

import pytest
from fastapi.testclient import TestClient

from potluck_local.app import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app()
    # client= sets request.client.host to 127.0.0.1 so the peer_access_middleware
    # treats this as a loopback caller and grants access to the full API surface.
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345)) as c:
        yield c


def test_gpu_test_endpoint_registered(client) -> None:
    """The endpoint exists and returns 200 (not 404/405)."""
    resp = client.get("/readiness/gpu-test")
    assert resp.status_code == 200


def test_gpu_test_response_shape(client) -> None:
    """All required keys are present with the right types."""
    body = client.get("/readiness/gpu-test").json()
    assert "ok" in body
    assert "device" in body
    assert "ran" in body
    assert "latency_ms" in body
    assert "detail" in body
    assert isinstance(body["ok"], bool)
    assert isinstance(body["device"], str)
    assert body["ran"] in ("inference", "compute", "none")
    assert isinstance(body["latency_ms"], (int, float))
    assert isinstance(body["detail"], str)


def test_gpu_test_ran_compute_when_no_model(client) -> None:
    """With no model loaded: ran should be 'compute' (torch present) or 'none'."""
    body = client.get("/readiness/gpu-test").json()
    # No model is loaded in tests; it must be compute or none.
    assert body["ran"] in ("compute", "none"), f"unexpected ran={body['ran']!r}"


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None,
    reason="torch not installed in this environment",
)
def test_gpu_test_ok_true_when_torch_present(client) -> None:
    """When torch is installed, the compute probe should succeed (ok=true)."""
    body = client.get("/readiness/gpu-test").json()
    assert body["ok"] is True, f"compute probe failed: {body['detail']}"
    assert body["ran"] == "compute"
    assert body["latency_ms"] >= 0


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None,
    reason="torch not installed in this environment",
)
def test_gpu_test_device_is_mps_or_cpu_on_mac(client) -> None:
    """On Apple Silicon macOS the device should be mps; cpu is accepted as fallback."""
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("not Apple Silicon macOS")

    body = client.get("/readiness/gpu-test").json()
    assert body["device"] in ("mps", "cpu"), f"unexpected device={body['device']!r}"


def test_create_app_still_torch_free_after_readiness_router() -> None:
    """Registering the readiness router must not drag torch in at import time.

    Runs in a subprocess for the same reason as test_app_routes.py: the shared
    pytest process may already have torch loaded by sibling tests.
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "from potluck_local.app import create_app\n"
        "create_app()\n"
        "assert 'torch' not in sys.modules, 'readiness router imported torch at import time'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"torch leaked at import.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
