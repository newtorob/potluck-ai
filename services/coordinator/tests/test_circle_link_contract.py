"""The direct circle link wire shapes still match the shared fixture.

tests/fixtures/circle_link_shapes.json is the meeting point of three sides: the
coordinator (this suite), the mesh sidecar (Go) and the engine (Python). The
sidecar and engine suites decode and re-encode the same examples. Here:

* every model the coordinator sends or receives has exactly the fixture's
  fields, and the example round-trips through it unchanged (so a rename, a new
  field or a changed default fails here, and the fixture, and with it the other
  two suites, must change with it);
* the additions to the attestation and the accept are a subset of the existing
  models (they have many other fields);
* shapes the coordinator neither sends nor reads (the sidecar's local API, the
  engine's listener, the receipt) are pinned to the key sets the design gives
  them, so the file cannot drift quietly;
* the examples agree with each other (one token, one hash, one link) and every
  shape has a note saying who sends it and who reads it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest

from coordinator.models.circle_link import END_REASONS
from coordinator.schemas.circle_link import (
    CircleLinkCreateRequest,
    CircleLinkCreateResponse,
    CircleLinkErrorBody,
    CircleLinkRenewResponse,
)
from coordinator.schemas.mesh import (
    CIRCLE_PEER_ACK_REASONS,
    CIRCLE_PEER_REMOVE_REASONS,
    SignalCirclePeerAck,
    SignalCirclePeerAdd,
    SignalCirclePeerExtend,
    SignalCirclePeerRemove,
    SignalHello,
)
from coordinator.schemas.peer import AcceptMessage, AttestationPayload
from coordinator.services.circle_links import LINK_FEATURE, hash_token

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "circle_link_shapes.json").read_text()
)
HTTP, SIGNAL = FIXTURE["http"], FIXTURE["signal"]
ATTEST, SIDECAR, ENGINE, RECEIPT = (
    FIXTURE["attestation"], FIXTURE["sidecar"], FIXTURE["engine"], FIXTURE["receipt"],
)


# ---- the fixture is complete and documented ---------------------------------


def _shape_paths() -> list[str]:
    """Every example in the fixture, as section.name (http.errors counts as one
    shape; so do the other grouped maps)."""
    grouped = {"http.errors", "http.errors_framework", "engine.errors"}
    paths: list[str] = []
    for section in ("vectors", "http", "signal", "attestation", "sidecar", "engine", "receipt"):
        body = FIXTURE[section]
        if section == "vectors":
            paths.append("vectors")
            continue
        for name in body:
            paths.append(f"{section}.{name}")
    assert grouped <= set(paths)
    return paths


@pytest.mark.parametrize("path", _shape_paths())
def test_every_shape_has_a_note_saying_who_sends_it_and_who_reads_it(path: str) -> None:
    note = FIXTURE["_notes"].get(path)
    assert isinstance(note, str) and note, f"_notes needs an entry for {path}"
    lowered = note.lower()
    assert "sender:" in lowered and "reader:" in lowered, f"{path}: say 'sender: ...' and 'reader: ...'"


def test_notes_name_no_shape_that_is_not_in_the_fixture() -> None:
    known = set(_shape_paths()) | {"_how_to_read", "enums", "rules"}
    stray = set(FIXTURE["_notes"]) - known
    assert not stray, f"notes for shapes that do not exist: {sorted(stray)}"


# ---- HTTP -------------------------------------------------------------------


def test_create_request_model_has_the_fixture_fields() -> None:
    example = HTTP["create_request"]
    assert set(CircleLinkCreateRequest.model_fields) == set(example)
    parsed = CircleLinkCreateRequest.model_validate(example)
    assert json.loads(parsed.model_dump_json()) == example
    minimal = CircleLinkCreateRequest.model_validate(HTTP["create_request_minimal"])
    assert minimal.exclude_link_ids == []


def test_create_request_caps_exclusions_and_requires_a_model() -> None:
    node = HTTP["create_request"]["requester_node_id"]
    with pytest.raises(ValueError):
        CircleLinkCreateRequest.model_validate(
            {"requester_node_id": node, "model_slug": "m", "exclude_link_ids": [str(uuid.uuid4())] * 5}
        )
    for bad in ("", "x" * 129):
        with pytest.raises(ValueError):
            CircleLinkCreateRequest.model_validate({"requester_node_id": node, "model_slug": bad})


@pytest.mark.parametrize("name,model", [
    ("create_response", CircleLinkCreateResponse),
    ("renew_response", CircleLinkRenewResponse),
])
def test_response_models_have_the_fixture_fields(name, model) -> None:
    example = HTTP[name]
    assert set(model.model_fields) == set(example)
    assert json.loads(model.model_validate(example).model_dump_json()) == example


def test_nested_response_fields_match_too() -> None:
    from coordinator.schemas.circle_link import CircleLinkDelivered, CircleLinkLender

    assert set(CircleLinkLender.model_fields) == set(HTTP["create_response"]["lender"])
    assert set(CircleLinkDelivered.model_fields) == set(HTTP["renew_response"]["delivered"])


@pytest.mark.parametrize("code", sorted(HTTP["errors"]))
def test_error_bodies_have_one_shape(code: str) -> None:
    entry = HTTP["errors"][code]
    assert set(entry) == {"status", "headers", "body"}
    body = entry["body"]
    assert set(body) == {"detail"} and set(body["detail"]) == {"code", "message", "reason"}
    assert body["detail"]["code"] == code
    assert json.loads(CircleLinkErrorBody.model_validate(body).model_dump_json()) == body


def test_error_reason_is_set_only_where_the_design_says() -> None:
    with_reason = {c for c, e in HTTP["errors"].items() if e["body"]["detail"]["reason"] is not None}
    assert with_reason == {"link_ended", "sidecar_rejected"}
    assert HTTP["errors"]["link_ended"]["body"]["detail"]["reason"] in END_REASONS
    assert HTTP["errors"]["sidecar_rejected"]["body"]["detail"]["reason"] in CIRCLE_PEER_ACK_REASONS


def test_error_statuses_are_the_designed_ones() -> None:
    assert {c: e["status"] for c, e in HTTP["errors"].items()} == {
        "circle_not_found": 404, "node_not_found": 404, "link_not_found": 404,
        "no_lender": 409, "requester_sidecar_offline": 409, "requester_sidecar_unsupported": 409,
        "link_conflict": 409, "link_ended": 410, "too_many_links": 429,
        "sidecar_rejected": 502, "links_disabled": 503, "sidecar_timeout": 504,
    }


# ---- signal -----------------------------------------------------------------


@pytest.mark.parametrize("name,model", [
    ("hello", SignalHello),
    ("circle_peer_add_lender", SignalCirclePeerAdd),
    ("circle_peer_add_requester", SignalCirclePeerAdd),
    ("circle_peer_add_requester_lan", SignalCirclePeerAdd),
    ("circle_peer_add_lender_relay_only", SignalCirclePeerAdd),
    ("circle_peer_add_requester_relay_only", SignalCirclePeerAdd),
    ("circle_peer_extend", SignalCirclePeerExtend),
    ("circle_peer_extend_relay_only", SignalCirclePeerExtend),
    ("circle_peer_remove", SignalCirclePeerRemove),
    ("circle_peer_ack_add_ready", SignalCirclePeerAck),
    ("circle_peer_ack_add_rejected", SignalCirclePeerAck),
    ("circle_peer_ack_extend_ready", SignalCirclePeerAck),
    ("circle_peer_ack_extend_rejected", SignalCirclePeerAck),
])
def test_signal_models_have_exactly_the_fixture_fields(name, model) -> None:
    example = SIGNAL[name]
    assert set(model.model_fields) == set(example), name
    # Unchanged through the model, values and all: what the coordinator sends is
    # what the sidecar's test decodes.
    assert model.model_validate(example).model_dump(mode="json") == example, name


def test_every_signal_message_type_is_distinct_and_named_as_the_design_says() -> None:
    assert {SIGNAL[n]["type"] for n in SIGNAL} == {
        "hello", "circle_peer_add", "circle_peer_extend", "circle_peer_remove", "circle_peer_ack",
    }


def test_add_roles_and_policy_in_the_examples() -> None:
    lender, requester = SIGNAL["circle_peer_add_lender"], SIGNAL["circle_peer_add_requester"]
    assert (lender["role"], requester["role"]) == ("lender", "requester")
    assert re.fullmatch(r"[0-9a-f]{64}", lender["token_sha256"])
    assert requester["token_sha256"] is None, "only the lender receives the hash"
    for name in ("circle_peer_add_lender_relay_only", "circle_peer_add_requester_relay_only"):
        relay = SIGNAL[name]
        assert (relay["direct_window_ms"], relay["peer_endpoint"], relay["peer_local_endpoints"]) == (0, None, [])
    lan = SIGNAL["circle_peer_add_requester_lan"]
    assert lan["peer_local_endpoints"] and lan["direct_window_ms"] > 0
    assert SIGNAL["circle_peer_extend_relay_only"]["peer_endpoint"] is None


def test_add_refuses_what_the_policy_and_roles_forbid() -> None:
    base = dict(SIGNAL["circle_peer_add_requester"])
    # The requester never gets a hash; the lender always does.
    with pytest.raises(ValueError):
        SignalCirclePeerAdd.model_validate({**base, "token_sha256": "a" * 64})
    with pytest.raises(ValueError):
        SignalCirclePeerAdd.model_validate({**SIGNAL["circle_peer_add_lender"], "token_sha256": None})
    # Relay-only carries no endpoint at all.
    with pytest.raises(ValueError):
        SignalCirclePeerAdd.model_validate({**base, "direct_window_ms": 0})
    # The peer's key is 32 bytes of canonical base64; its address is in the pool.
    for bad in (
        {"peer_wg_public_key": "short"},
        {"peer_wg_public_key": "A" * 44},
        {"peer_mesh_ip": "10.0.0.5"},
        {"peer_mesh_ip": "100.64.0.7\n"},
        {"peer_mesh_ip": "::1"},
        {"port": 0},
        {"port": 65536},
        {"ttl_s": 4},
        {"ttl_s": 1801},
        {"direct_window_ms": 10001},
        {"peer_endpoint": "1.2.3.4:5\nallowed_ip=0.0.0.0/0"},
    ):
        with pytest.raises(ValueError):
            SignalCirclePeerAdd.model_validate({**base, **bad})


def test_add_refilters_local_endpoints_to_private_lan_addresses() -> None:
    base = dict(SIGNAL["circle_peer_add_requester_lan"])
    msg = SignalCirclePeerAdd.model_validate(
        {**base, "peer_local_endpoints": [
            "192.168.1.23:51820", "8.8.8.8:51820", "100.64.1.2:51820", "127.0.0.1:1", "junk",
            "192.168.1.23:51820",
        ]}
    )
    assert msg.peer_local_endpoints == ["192.168.1.23:51820"]


def test_remove_and_ack_reason_sets_are_the_designed_ones() -> None:
    assert set(CIRCLE_PEER_REMOVE_REASONS) == set(FIXTURE["enums"]["remove_reason"])
    assert set(CIRCLE_PEER_ACK_REASONS) == set(FIXTURE["enums"]["ack_reason"])
    assert FIXTURE["enums"]["feature"] == [LINK_FEATURE]
    assert FIXTURE["enums"]["role"] == ["lender", "requester"]
    for reason in FIXTURE["enums"]["remove_reason"]:
        SignalCirclePeerRemove(link_id=uuid.uuid4(), reason=reason)
    with pytest.raises(ValueError):
        SignalCirclePeerRemove(link_id=uuid.uuid4(), reason="member_left")


def test_hello_features_are_read_leniently() -> None:
    """A bad features value must never fail the hello: that closes the node's
    whole signal channel, and with it its household's mesh."""
    key = SIGNAL["hello"]["wg_public_key"]
    cases = [
        (["circle_link_v1"], ["circle_link_v1"]),
        (None, []),
        ("circle_link_v1", []),
        ({"circle_link_v1": True}, []),
        (7, []),
        ([None, 3, ["x"], {"a": 1}, "circle_link_v1", "Has Space", "UPPER", "", "x" * 41], ["circle_link_v1"]),
        (["a", "a", "a"], ["a"]),
        ([f"f{i}" for i in range(40)], [f"f{i}" for i in range(16)]),
    ]
    for given, want in cases:
        assert SignalHello.model_validate({"type": "hello", "wg_public_key": key, "features": given}).features == want, given
    # And a hello with no features at all (an old sidecar) still parses.
    assert SignalHello.model_validate({"type": "hello", "wg_public_key": key}).features == []


# ---- attestation ------------------------------------------------------------


def test_attestation_additions_are_optional_fields_of_the_existing_models() -> None:
    assert set(ATTEST["attest_added"]) <= set(AttestationPayload.model_fields)
    assert set(ATTEST["accept_added"]) <= set(AcceptMessage.model_fields)
    assert set(ATTEST["accept_added_unbound"]) <= set(AcceptMessage.model_fields)
    assert set(ATTEST["attest_added"]) == {"mesh_node_id", "circle_port"}
    assert set(ATTEST["accept_added"]) == {"mesh_node_id", "circle_links"}
    assert ATTEST["accept_links_off"] == {}
    for name in ("mesh_node_id", "circle_port"):
        assert not AttestationPayload.model_fields[name].is_required()
    for name in ("mesh_node_id", "circle_links"):
        assert not AcceptMessage.model_fields[name].is_required()


def _attest_body(**extra) -> dict:
    return {
        "type": "attest", "member_id": str(uuid.uuid4()), "peer_public_key": "k", "binary_hash": "h",
        "client_version": "0.0.0", "platform": "p", "nonce_signature": "s", **extra,
    }


def test_attestation_accepts_the_fixture_additions_and_their_absence() -> None:
    parsed = AttestationPayload.model_validate(_attest_body(**ATTEST["attest_added"]))
    assert str(parsed.mesh_node_id) == ATTEST["attest_added"]["mesh_node_id"]
    assert parsed.circle_port == ATTEST["attest_added"]["circle_port"]
    old = AttestationPayload.model_validate(_attest_body())
    assert (old.mesh_node_id, old.circle_port) == (None, None)


@pytest.mark.parametrize("bad", [
    {"mesh_node_id": "not-a-uuid"}, {"mesh_node_id": 7}, {"mesh_node_id": True},
    {"mesh_node_id": ["x"]}, {"mesh_node_id": {"a": 1}},
    {"circle_port": 0}, {"circle_port": 65536}, {"circle_port": -1}, {"circle_port": "8340"},
    {"circle_port": 8340.5}, {"circle_port": True}, {"circle_port": None}, {"circle_port": [1]},
])
def test_a_bad_binding_value_never_rejects_the_attestation(bad: dict) -> None:
    """A reject takes the machine off the relay, so these read as absent."""
    parsed = AttestationPayload.model_validate(_attest_body(**bad))
    for name in bad:
        assert getattr(parsed, name) is None


def test_accept_carries_the_fixture_additions() -> None:
    base = {
        "peer_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
        "server_time": "2026-10-09T12:00:00Z",
    }
    dumped = AcceptMessage.model_validate({**base, **ATTEST["accept_added"]}).model_dump(mode="json")
    for key, value in ATTEST["accept_added"].items():
        assert dumped[key] == value


# ---- examples agree with each other ------------------------------------------


def test_one_token_one_hash_one_link_across_the_examples() -> None:
    assert hashlib.sha256(FIXTURE["vectors"]["token"].encode()).hexdigest() == FIXTURE["vectors"]["token_sha256"]
    assert hash_token(FIXTURE["vectors"]["token"]) == FIXTURE["vectors"]["token_sha256"]

    token = HTTP["create_response"]["token"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token), "32 random bytes, urlsafe base64, no padding"
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    digest = hash_token(token)
    assert SIGNAL["circle_peer_add_lender"]["token_sha256"] == digest
    assert SIGNAL["circle_peer_add_lender_relay_only"]["token_sha256"] == digest
    inbound = SIDECAR["circle_peers"]["peers"][0]["inbound"]
    assert inbound["token_sha256"] == digest

    link = HTTP["create_response"]["link_id"]
    for name in ("circle_peer_add_lender", "circle_peer_add_requester", "circle_peer_extend",
                 "circle_peer_remove", "circle_peer_ack_add_ready"):
        assert SIGNAL[name]["link_id"] == link, name
    assert inbound["link_id"] == link
    assert HTTP["renew_response"]["link_id"] == link
    assert HTTP["create_response"]["lender"]["port"] == SIGNAL["circle_peer_add_requester"]["port"]
    assert HTTP["create_response"]["lender"]["mesh_ip"] == SIGNAL["circle_peer_add_requester"]["peer_mesh_ip"]
    assert HTTP["create_response"]["lender"]["port"] == inbound["port"]
    assert HTTP["create_response"]["renew_after_s"] == HTTP["create_response"]["ttl_s"] // 2
    assert ATTEST["attest_added"]["mesh_node_id"] == HTTP["create_request"]["requester_node_id"]


# ---- shapes the coordinator neither sends nor reads --------------------------


def test_sidecar_local_api_shapes() -> None:
    assert SIDECAR["status_addition"] == {"features": [LINK_FEATURE]}
    peers = SIDECAR["circle_peers"]["peers"]
    assert set(SIDECAR["circle_peers"]) == {"peers"}
    assert len(peers) >= 2, "one inbound-only and one outbound-only entry"
    for entry in peers:
        assert set(entry) == {
            "scope", "mesh_ip", "reachable", "reachability_mode", "handshake_age_seconds",
            "inbound", "outbound",
        }
        assert entry["scope"] == "circle"
        assert entry["reachability_mode"] in FIXTURE["enums"]["reachability_mode"]
        # A stranger's address, name, id or key must never be shown.
        assert not ({"node_name", "node_id", "last_endpoint", "local_endpoints", "wg_public_key"} & set(entry))
    inbound = peers[0]["inbound"]
    assert set(inbound) == {"link_id", "port", "token_sha256", "expires_in_s"}
    assert peers[0]["outbound"] is None
    outbound = peers[1]["outbound"]
    assert set(outbound) == {"link_id", "port", "expires_in_s"}
    assert peers[1]["inbound"] is None


def test_engine_listener_shapes() -> None:
    assert set(ENGINE["capabilities"]) == {
        "schemaVersion", "protocol_version", "available_for_inference", "loaded_model", "busy",
        "in_flight", "max_concurrent_jobs",
    }
    request = ENGINE["chat_request"]
    assert set(request) == {"messages", "model", "stream", "temperature", "max_tokens", "stop"}
    assert request["stream"] is True and 1 <= request["max_tokens"] <= 4096
    assert all(set(m) == {"role", "content"} and m["role"] in {"system", "user", "assistant"}
               for m in request["messages"])
    assert not ({"potluck_batch", "potluck_model_sha256"} & set(request))

    chunk, meta, done = ENGINE["sse"]
    assert chunk.startswith("data: ") and done == "data: [DONE]"
    meta_body = json.loads(meta.removeprefix("data: "))["meta"]
    assert set(meta_body) == {"served_by", "served_scope", "output_tokens", "duration_ms", "finish_reason"}
    assert meta_body["served_scope"] == "circle"
    assert json.loads(chunk.removeprefix("data: "))["choices"][0]["delta"]

    assert {c: e["status"] for c, e in ENGINE["errors"].items()} == {
        "bad_token": 401, "not_a_circle_peer": 403, "model_mismatch": 409, "busy": 429,
        "not_sharing": 503, "no_model": 503,
    }
    for code, entry in ENGINE["errors"].items():
        assert set(entry) == {"status", "headers", "body"}
        assert entry["body"] == {"detail": {"code": code, "message": entry["body"]["detail"]["message"]}}
    assert ENGINE["errors"]["busy"]["headers"] == {"Retry-After": "2"}


def test_receipt_shapes() -> None:
    keys = {"served_by", "served_scope", "path", "transport", "output_tokens", "duration_ms", "finish_reason"}
    assert set(RECEIPT["meta_direct"]) == keys == set(RECEIPT["meta_relay"])
    assert RECEIPT["meta_direct"]["path"] == "direct" and RECEIPT["meta_relay"]["path"] == "relay"
    assert RECEIPT["meta_direct"]["transport"] in {"udp", "derp", None}
    assert RECEIPT["meta_relay"]["transport"] is None
    # What the lender streamed is what the receipt reports.
    streamed = json.loads(ENGINE["sse"][1].removeprefix("data: "))["meta"]
    for field in ("output_tokens", "duration_ms", "finish_reason"):
        assert RECEIPT["meta_direct"][field] == streamed[field]
