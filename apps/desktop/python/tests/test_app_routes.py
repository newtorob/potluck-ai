"""App-factory tests: route registration, /health shape, and the lazy-torch invariant.

These are tests over create_app() itself. They are fast: no model
downloads, no peers, no network - just the FastAPI TestClient over the in-process app.

The key assertion here is that create_app() does not import torch. The base
sidecar/bundle must stay light; torch + transformers are only ever imported lazily, on
the path that actually runs a pipeline stage. A regression that top-imports the engine
(or anything that drags torch in) from a router module would bloat the bundle and slow
cold start - this guards that discipline.
"""

from __future__ import annotations

import subprocess
import sys

from fastapi.testclient import TestClient

from potluck_local.app import create_app


def test_create_app_registers_expected_routes() -> None:
    """create_app() wires up the routes the frontend + peers depend on."""
    app = create_app()
    # (method, path) pairs registered on the app.
    registered: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            for m in methods:
                registered.add((m, path))

    paths = {p for _, p in registered}
    assert "/health" in paths
    assert "/peer/capabilities" in paths
    assert "/peer/pipeline/stage" in paths
    # The pipeline-stage endpoint is the full POST/GET/DELETE lifecycle (start,
    # status, teardown) the orchestrator drives on a household peer.
    assert ("POST", "/peer/pipeline/stage") in registered
    assert ("GET", "/peer/pipeline/stage") in registered
    assert ("DELETE", "/peer/pipeline/stage") in registered
    # Model load is templated (/models/{slug}/load); assert the templated form + verb.
    assert ("POST", "/models/{slug}/load") in registered


def test_create_app_does_not_import_torch() -> None:
    """create_app() must stay torch-free - the engine is imported lazily.

    Asserted in a fresh subprocess on purpose: pytest collects every test module in
    one interpreter, and a sibling test (e.g. the placement tests) top-imports torch,
    which would poison a `'torch' in sys.modules` check in this shared process. The
    subprocess proves the property in isolation - exactly the state a cold sidecar
    boot is in.
    """
    code = (
        "import sys\n"
        "from potluck_local.app import create_app\n"
        "create_app()\n"
        "assert 'torch' not in sys.modules, 'create_app imported torch'\n"
        "assert 'transformers' not in sys.modules, 'create_app imported transformers'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"create_app imported a heavy dep.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_health_shape() -> None:
    """/health includes the loaded_pipeline + loaded_rpc_servers status keys."""
    app = create_app()
    # The frontend polls /health from loopback. A caller that is not a household
    # member (TestClient's default "testclient" host is one) gets only the
    # liveness stub; see tests/test_peer_trust.py.
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Both "big model" status surfaces must be present (None when nothing is
        # loaded that way). The frontend reads these as the source of truth for the
        # RPC / pipeline toggles, so their absence is a contract break.
        assert "loaded_pipeline" in body
        assert "loaded_rpc_servers" in body
        # Shared (continuous) batching flag - the frontend reads it as the source of
        # truth for the shared-batching toggle. False when nothing is loaded that way.
        assert "continuous_batch" in body
        assert body["continuous_batch"] is False


def test_load_request_accepts_continuous_batch() -> None:
    """The load request body model accepts continuous_batch without a validation
    error and defaults it to False - the wire contract the frontend depends on.

    Validated at the Pydantic-model boundary (not by actually loading a model, which
    needs torch + a model on disk): a malformed/absent field would 422 the request
    before the runtime is ever touched.
    """
    from potluck_local.routers.models import LoadModelRequest

    assert LoadModelRequest().continuous_batch is False
    assert LoadModelRequest(continuous_batch=True).continuous_batch is True
    # Round-trips through JSON the same way the frontend sends it.
    assert LoadModelRequest.model_validate({"continuous_batch": True}).continuous_batch is True
