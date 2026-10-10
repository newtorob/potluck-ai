"""The engine against the wire contract for direct circle links.

services/coordinator/tests/fixtures/circle_link_shapes.json is shared by the
coordinator, the mesh sidecar and this engine. Every shape the engine sends or
reads is pinned to it here: what it sends must equal the fixture's example, and
what it reads must parse from the fixture's example without losing a field.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from circle_support import FIXTURE

from potluck_local.contributor.schemas import AcceptMessage, AttestationPayload
from potluck_local.routers import circle_link as router
from potluck_local.services import circle_gate, circle_link, circle_mesh
from potluck_local.services.remote_stream import openai_chunks

COORDINATOR = Path(__file__).resolve().parents[4] / "services" / "coordinator" / "coordinator"
HTTP = FIXTURE["http"]
SIDECAR = FIXTURE["sidecar"]
ENGINE = FIXTURE["engine"]


# ---- the hash ---------------------------------------------------------------------


def test_the_token_hash_is_the_vectors():
    vectors = FIXTURE["vectors"]
    assert hashlib.sha256(vectors["token"].encode("ascii")).hexdigest() == vectors["token_sha256"]
    assert circle_gate.token_digest(vectors["token"]) == vectors["token_sha256"]


def test_a_token_is_hashed_as_the_text_sent_never_as_the_bytes_it_decodes_to():
    token = HTTP["create_response"]["token"]
    sent_as_text = hashlib.sha256(token.encode("ascii")).hexdigest()
    assert circle_gate.token_digest(token) == sent_as_text
    assert sent_as_text == FIXTURE["signal"]["circle_peer_add_lender"]["token_sha256"]
    assert sent_as_text == SIDECAR["circle_peers"]["peers"][0]["inbound"]["token_sha256"]
    import base64

    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert hashlib.sha256(decoded).hexdigest() != sent_as_text


# ---- what the engine sends to the coordinator -------------------------------------------


def test_the_create_body_is_the_fixtures():
    example = HTTP["create_request"]
    assert circle_link.create_body(example["requester_node_id"], example["model_slug"],
                                   example["exclude_link_ids"]) == example
    minimal = HTTP["create_request_minimal"]
    assert circle_link.create_body(minimal["requester_node_id"], minimal["model_slug"], []) == minimal


def test_the_renew_body_is_empty():
    assert HTTP["renew_request"] == {}


# ---- what the engine reads from the coordinator -------------------------------------------


def test_the_create_response_parses_and_the_model_has_the_fixtures_fields():
    example = HTTP["create_response"]
    parsed = circle_link.LinkCreated.model_validate(example)
    assert set(circle_link.LinkCreated.model_fields) == set(example)
    assert set(circle_link._Lender.model_fields) == set(example["lender"])
    assert parsed.lender.port == example["lender"]["port"] and parsed.token == example["token"]
    assert parsed.ready is True and parsed.renew_after_s * 2 == parsed.ttl_s


def test_the_renew_response_parses_and_the_model_has_the_fixtures_fields():
    example = HTTP["renew_response"]
    circle_link.LinkRenewed.model_validate(example)
    assert set(circle_link.LinkRenewed.model_fields) == set(example)


def test_a_newer_coordinator_may_add_fields():
    circle_link.LinkCreated.model_validate({**HTTP["create_response"], "future_field": 1})
    circle_link.LinkRenewed.model_validate({**HTTP["renew_response"], "future_field": 1})


@pytest.mark.parametrize("name", sorted(HTTP["errors"]))
def test_error_codes_are_read_from_detail_code(name):
    error = HTTP["errors"][name]
    response = httpx.Response(error["status"], json=error["body"], headers=error["headers"])
    assert circle_link._detail_code(response) == error["body"]["detail"]["code"]


@pytest.mark.parametrize("name", sorted(HTTP["errors_framework"]))
def test_the_frameworks_own_error_bodies_have_no_code(name):
    error = HTTP["errors_framework"][name]
    assert circle_link._detail_code(httpx.Response(error["status"], json=error["body"])) is None


def test_pool_dispatch_error_flattens_a_dict_detail_so_it_cannot_be_branched_on():
    from potluck_local.services.pool_routing import PoolDispatchError

    exc = PoolDispatchError(410, json.dumps(HTTP["errors"]["link_ended"]["body"]))
    assert isinstance(exc.detail, str) and exc.detail.startswith("{'code': 'link_ended'")


def test_retry_after_is_read_from_the_header():
    too_many = HTTP["errors"]["too_many_links"]
    response = httpx.Response(too_many["status"], json=too_many["body"], headers=too_many["headers"])
    assert circle_link._retry_after(response) == 12.0
    assert circle_link._retry_after(httpx.Response(429)) == 5.0
    assert circle_link._retry_after(httpx.Response(429, headers={"Retry-After": "0"})) == 1.0
    assert circle_link._retry_after(httpx.Response(429, headers={"Retry-After": "99999"})) == 120.0
    assert circle_link._retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026"})) == 5.0


# ---- the sidecar ------------------------------------------------------------------------------


def test_the_status_addition_is_read():
    status = circle_mesh.parse_status({"node_id": "3b1f0c52-7d1e-4a55-9a0e-1f2b3c4d5e6f", "mesh_ip": "100.64.0.4",
                                       **SIDECAR["status_addition"]})
    assert status is not None and status.supports_links
    assert status.features == frozenset(FIXTURE["enums"]["feature"])
    assert circle_mesh.LINK_FEATURE in FIXTURE["enums"]["feature"]
    without_node = circle_mesh.parse_status({"node_id": None, **SIDECAR["status_addition"]})
    assert without_node is not None and not without_node.supports_links
    assert not circle_mesh.parse_status({"node_id": "3b1f0c52-7d1e-4a55-9a0e-1f2b3c4d5e6f"}).supports_links


def test_circle_peers_are_read_with_every_field():
    peers = circle_mesh.parse_circle_peers(SIDECAR["circle_peers"])
    assert peers is not None and len(peers) == 2
    lending, borrowing = peers
    raw_lending, raw_borrowing = SIDECAR["circle_peers"]["peers"]
    assert str(lending.mesh_ip) == raw_lending["mesh_ip"] and lending.reachable is True and lending.mode == "direct"
    assert lending.inbound is not None and lending.outbound is None
    assert lending.inbound.link_id == raw_lending["inbound"]["link_id"]
    assert lending.inbound.port == raw_lending["inbound"]["port"]
    assert lending.inbound.token_sha256 == raw_lending["inbound"]["token_sha256"]
    assert lending.inbound.expires_in_s == raw_lending["inbound"]["expires_in_s"]
    assert borrowing.reachable is False and borrowing.mode == "connecting"
    assert borrowing.inbound is None and borrowing.outbound is not None
    assert borrowing.outbound.link_id == raw_borrowing["outbound"]["link_id"]
    assert borrowing.outbound.port == raw_borrowing["outbound"]["port"]


def test_the_reachability_modes_are_the_contracts():
    assert list(circle_mesh.REACHABILITY_MODES) + [None] == FIXTURE["enums"]["reachability_mode"]


def test_the_gate_builds_its_view_from_the_fixtures_own_bodies():
    state = circle_gate.parse_state({"mesh_ip": "100.64.1.2", "node_id": None}, SIDECAR["circle_peers"])
    inbound_ip = SIDECAR["circle_peers"]["peers"][0]["mesh_ip"]
    assert state is not None
    assert {str(ip): digest for ip, digest in state.inbound.items()} == {
        inbound_ip: SIDECAR["circle_peers"]["peers"][0]["inbound"]["token_sha256"]}, \
        "only the reachable inbound entry may call; the unreachable outbound one may not"


# ---- the attestation ----------------------------------------------------------------------------


def test_the_attestation_additions_are_the_contracts():
    added = FIXTURE["attestation"]["attest_added"]
    assert set(added) == {"mesh_node_id", "circle_port"}
    payload = AttestationPayload(member_id="00000000-0000-0000-0000-000000000001", peer_public_key="k",
                                 binary_hash="b", client_version="v", platform="p", nonce_signature="s", **added)
    sent = json.loads(payload.model_dump_json())
    assert {k: sent[k] for k in added} == added


def test_the_accept_additions_are_the_contracts():
    base = {"type": "accept", "peer_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002", "server_time": "2026-10-09T00:00:00Z",
            "heartbeat_interval_s": 30}
    for name, expect_links in (("accept_added", True), ("accept_added_unbound", True), ("accept_links_off", None)):
        parsed = AcceptMessage.model_validate({**base, **FIXTURE["attestation"][name]})
        assert parsed.circle_links is expect_links, name
    assert AcceptMessage.model_validate({**base, **FIXTURE["attestation"]["accept_added"]}).mesh_node_id is not None


# ---- the engine's own listener -----------------------------------------------------------------------


def test_the_chat_request_is_the_fixtures_both_ways():
    example = ENGINE["chat_request"]
    sent = circle_link.chat_body(example["messages"], example["model"], example["temperature"], example["max_tokens"])
    assert sent == example
    router.CircleChatRequest.model_validate(example)
    assert set(router.CircleChatRequest.model_fields) == set(example)
    assert set(router.CircleMessage.model_fields) == set(example["messages"][0])


def test_the_listeners_limits_are_the_requesters():
    assert circle_link._MAX_MESSAGES == router.MAX_MESSAGES
    assert circle_link._MAX_BODY_BYTES == router.MAX_BODY_BYTES
    assert circle_link._MAX_OUTPUT_TOKENS == router.MAX_OUTPUT_TOKENS
    assert circle_link.CHAT_PATH == router.CHAT_PATH == "/circle/chat/completions"
    assert circle_link._ROLES == {"system", "user", "assistant"}
    assert set(router.CircleMessage.model_fields["role"].annotation.__args__) == circle_link._ROLES


def test_the_output_ceiling_is_the_coordinators():
    tree = ast.parse((COORDINATOR / "schemas" / "peer.py").read_text())
    ceiling = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "MAX_OUTPUT_TOKENS" for t in n.targets))
    assert ceiling == router.MAX_OUTPUT_TOKENS


def test_the_first_token_budget_constants_are_the_coordinators():
    tree = ast.parse((COORDINATOR / "routers" / "jobs.py").read_text())
    values = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              for t in n.targets if isinstance(t, ast.Name)}
    assert circle_link.FIRST_TOKEN_FLOOR_S == values["FIRST_TOKEN_DEADLINE_S"]
    assert circle_link.FIRST_TOKEN_MAX_S == values["FIRST_TOKEN_MAX_S"]
    assert circle_link.PROMPT_TOKENS_PER_SECOND == values["_TIER_PP_TOKENS_PER_SECOND"]
    assert circle_link.CHARS_PER_TOKEN == values["_CHARS_PER_TOKEN"]


@pytest.fixture()
def listener(fake_mesh):
    from test_circle_gate import Lender

    return Lender(fake_mesh)


def test_the_capabilities_response_is_the_fixtures(listener):
    from test_circle_gate import TOKEN, auth

    body = listener.client().get("/circle/capabilities", headers=auth(TOKEN)).json()
    assert body == ENGINE["capabilities"]
    assert {k: type(v) for k, v in body.items()} == {k: type(v) for k, v in ENGINE["capabilities"].items()}


@pytest.mark.parametrize("name", sorted(ENGINE["errors"]))
def test_each_error_the_listener_gives_is_the_fixtures(listener, name):
    from test_circle_gate import CHAT, TAILSCALE, TOKEN, auth

    expected = ENGINE["errors"][name]
    if name == "bad_token":
        resp = listener.client().post("/circle/chat/completions", json=CHAT, headers=auth("wrong"))
    elif name == "not_a_circle_peer":
        resp = listener.client(TAILSCALE).post("/circle/chat/completions", json=CHAT, headers=auth(TOKEN))
    elif name == "model_mismatch":
        resp = listener.client().post("/circle/chat/completions", json={**CHAT, "model": "other"}, headers=auth())
    elif name == "busy":
        listener.cap = 0
        resp = listener.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    elif name == "not_sharing":
        listener.refusal = "not_sharing_circles"
        resp = listener.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    elif name == "model_not_verified":
        listener.runtime.verified = False
        resp = listener.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    else:
        assert name == "no_model"
        listener.runtime.model = None
        resp = listener.client().post("/circle/chat/completions", json=CHAT, headers=auth())
    assert resp.status_code == expected["status"], name
    assert resp.json() == expected["body"], name
    for header, value in expected["headers"].items():
        assert resp.headers[header] == value, name


def test_the_requester_reads_the_fixtures_stream():
    class Response:
        def __init__(self, lines):
            self.lines = lines

        async def aiter_lines(self):
            for line in self.lines:
                yield line
                yield ""

    async def read():
        return [c async for c in openai_chunks(Response(ENGINE["sse"]), "m", coordinator=True)]

    chunks = asyncio.run(read())
    assert chunks[0]["choices"][0]["delta"] == {"content": "Hello"}
    meta = json.loads(ENGINE["sse"][1][6:])["meta"]
    assert chunks[-1]["potluck"] == {"finish_reason": meta["finish_reason"], "served_scope": "circle",
                                     "output_tokens": meta["output_tokens"], "duration_ms": meta["duration_ms"]}


def test_every_error_the_listener_gives_is_a_failure_before_the_first_token():
    # engine.errors: "Before the first token any of these means 'try another lender, then the relay'."
    assert ENGINE["errors"].keys() == {"bad_token", "not_a_circle_peer", "model_mismatch", "busy", "not_sharing", "no_model",
                                       "model_not_verified"}


# ---- the receipt -----------------------------------------------------------------------------------------


def test_the_receipts_are_the_fixtures():
    lender_meta = json.loads(ENGINE["sse"][1][6:])["meta"]
    direct = FIXTURE["receipt"]["meta_direct"]
    assert circle_link.receipt(lender_meta, path="direct", transport="derp") == direct
    relay = FIXTURE["receipt"]["meta_relay"]
    assert circle_link.receipt(lender_meta, path="relay", transport=None) == relay
    assert set(direct) == set(relay)


def test_the_receipt_transport_follows_the_contracts_wording():
    # "udp for direct, derp for the relay, null if unknown"
    assert circle_link.transport_of("direct") == "udp"
    assert circle_link.transport_of("derp") == "derp"
    assert circle_link.transport_of("connecting") is None
    assert circle_link.transport_of(None) is None
