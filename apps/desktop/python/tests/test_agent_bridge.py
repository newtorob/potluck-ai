"""Provider hook normalization and local agent-run persistence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from io import StringIO

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.agent_bridge import (
    NormalizedEvent,
    ResourceRef,
    ingest_payload,
    normalize_hook_event,
    write_event,
)
from potluck_local.routers.agents import router
from potluck_local.services.agent_connectors import (
    AgentConnectorError,
    connect_provider,
    connector_status,
    disconnect_provider,
)
from potluck_local.services.memory_db import init_memory_db
from potluck_local.services.task_state import get_task_manager


def _init(db_path) -> None:
    asyncio.run(init_memory_db(db_path))


def test_normalizer_keeps_only_safe_codex_metadata(tmp_path) -> None:
    raw = {
        "session_id": "codex-session-1",
        "hook_event_name": "PreToolUse",
        "turn_id": "turn-1",
        "tool_use_id": "tool-1",
        "tool_name": "apply_patch",
        "cwd": str(tmp_path),
        "model": "gpt-5.6",
        "prompt": "PRIVATE PROMPT",
        "tool_input": {
            "patch": "*** Update File: src/app.py\n@@\n-secret\n+API_KEY=secret",
            "command": "cat ~/.ssh/id_rsa",
        },
        "tool_response": "PRIVATE RESULT",
        "transcript_path": "/private/transcript.jsonl",
    }

    event = normalize_hook_event("codex", raw, now="2026-09-03T12:00:00+00:00")

    assert event is not None
    assert event.event_type == "tool.started"
    assert event.attribution_confidence == "unknown"
    assert event.metadata == {"tool_category": "file"}
    assert [(item.kind, item.ref, item.action) for item in event.resources] == [
        ("file", "src/app.py", "write")
    ]
    serialized = json.dumps(event.__dict__, default=str)
    assert "PRIVATE" not in serialized
    assert "API_KEY" not in serialized
    assert "id_rsa" not in serialized
    assert "transcript" not in serialized


def test_claude_subagent_tool_has_exact_lane(tmp_path) -> None:
    event = normalize_hook_event(
        "claude",
        {
            "session_id": "claude-session-1",
            "hook_event_name": "PostToolUse",
            "prompt_id": "prompt-1",
            "tool_use_id": "tool-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / "src" / "view.tsx")},
            "cwd": str(tmp_path),
            "agent_id": "agent-explorer",
            "agent_type": "Explore",
        },
        now="2026-09-03T12:00:01+00:00",
    )

    assert event is not None
    assert event.provider_agent_id == "agent-explorer"
    assert event.agent_label == "Explore"
    assert event.attribution_confidence == "exact"
    assert event.resources[0].ref == "src/view.tsx"


def test_claude_current_model_switch_event_is_normalized(tmp_path) -> None:
    event = normalize_hook_event(
        "claude",
        {
            "session_id": "claude-session-1",
            "hook_event_name": "PostModelSwitch",
            "cwd": str(tmp_path),
            "from_model": "sonnet",
            "to_model": "opus",
        },
        now="2026-09-04T12:00:00+00:00",
    )

    assert event is not None
    assert event.event_type == "model.changed"
    assert event.model == "opus"
    assert event.metadata == {"from_model": "sonnet", "to_model": "opus"}


def test_bridge_persists_deduplicated_run_and_duration(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    _init(db_path)
    base = {
        "session_id": "codex-session-1",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "model": "gpt-5.6",
    }
    assert ingest_payload(
        "codex", {**base, "hook_event_name": "SessionStart", "source": "startup"}, db_path
    )

    started = normalize_hook_event(
        "codex",
        {
            **base,
            "hook_event_name": "PreToolUse",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo TOP_SECRET"},
        },
        now="2026-09-03T12:00:00+00:00",
    )
    completed = normalize_hook_event(
        "codex",
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo TOP_SECRET"},
            "tool_response": "TOP_SECRET",
        },
        now="2026-09-03T12:00:01.250000+00:00",
    )
    assert started is not None and completed is not None

    from potluck_local.agent_bridge import write_event

    assert write_event(started, db_path)
    assert write_event(completed, db_path)
    assert not write_event(completed, db_path)

    with sqlite3.connect(db_path) as conn:
        run = conn.execute(
            "SELECT provider, project, capture_profile FROM agent_runs"
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, duration_ms, metadata FROM agent_events ORDER BY seq"
        ).fetchall()
        raw_db_text = " ".join(str(item) for row in events for item in row)

    assert run == ("codex", tmp_path.name, "metadata-only")
    assert [row[0] for row in events] == ["run.started", "tool.started", "tool.completed"]
    assert events[-1][1] == 1250
    assert "TOP_SECRET" not in raw_db_text


def test_managed_event_links_run_to_task_without_changing_observed_contract(tmp_path) -> None:
    db_path = tmp_path / "memory.db"
    _init(db_path)
    event = NormalizedEvent(
        provider="codex",
        provider_session_id="managed-thread-1",
        provider_event_name="thread/start",
        event_type="run.started",
        status="started",
        project="potluck",
        model="gpt-5.6",
        tool_name=None,
        turn_id=None,
        correlation_id="managed-thread-1",
        provider_agent_id=None,
        agent_kind="root",
        agent_label="Root agent",
        attribution_confidence="exact",
        occurred_at="2026-09-05T12:00:00+00:00",
        metadata={},
        resources=(ResourceRef(kind="task", ref="task-1", action="read"),),
    )

    assert write_event(
        event,
        db_path,
        capture_profile="managed-local",
        capture_note="Managed metadata only.",
        control_mode="managed",
        task_id="task-1",
        cwd=str(tmp_path),
    )

    with sqlite3.connect(db_path) as conn:
        run = conn.execute(
            "SELECT capture_profile, control_mode, task_id, cwd FROM agent_runs"
        ).fetchone()
        resource = conn.execute(
            "SELECT kind, resource_ref, action FROM agent_resources"
        ).fetchone()

    assert run == ("managed-local", "managed", "task-1", str(tmp_path))
    assert resource == ("task", "task-1", "read")


def test_run_api_returns_lanes_events_and_cursor(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    _init(db_path)
    assert ingest_payload(
        "claude",
        {
            "session_id": "claude-session-1",
            "hook_event_name": "SubagentStart",
            "prompt_id": "prompt-1",
            "agent_id": "agent-reviewer",
            "agent_type": "Reviewer",
            "cwd": str(tmp_path),
        },
        db_path,
    )

    app = FastAPI()
    app.include_router(router, prefix="/agents")
    client = TestClient(app)

    runs_response = client.get("/agents/runs")
    assert runs_response.status_code == 200
    runs = runs_response.json()
    assert len(runs) == 1
    assert runs[0]["agent_count"] == 2

    detail_response = client.get(f"/agents/runs/{runs[0]['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert [instance["kind"] for instance in detail["instances"]] == ["root", "subagent"]
    assert detail["events"][0]["event_type"] == "agent.started"
    assert detail["next_cursor"] == detail["events"][0]["seq"]

    delta = client.get(
        f"/agents/runs/{runs[0]['id']}/events",
        params={"after_seq": detail["next_cursor"]},
    ).json()
    assert delta["events"] == []
    assert delta["resources"] == []
    assert delta["next_cursor"] == detail["next_cursor"]
    assert [instance["kind"] for instance in delta["instances"]] == ["root", "subagent"]


def test_managed_api_builds_task_context_and_exposes_controls(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    _init(db_path)
    task = asyncio.run(
        get_task_manager().create_task(
            task_name="Ship workroom",
            objective="Control Codex from Tasks",
            known_constraints=["Keep traces metadata only"],
            pending_steps=["Build launch flow"],
            project="potluck",
        )
    )

    captured: dict[str, object] = {}
    payload = {
        "id": "thread-1",
        "run_id": "run-1",
        "provider": "codex",
        "control_mode": "managed",
        "task_id": task.id,
        "task_name": task.task_name,
        "cwd": str(tmp_path),
        "project": "potluck",
        "model": None,
        "sandbox": "workspace-write",
        "status": "running",
        "active_turn_id": "turn-1",
        "started_at": "2026-09-05T12:00:00+00:00",
        "updated_at": "2026-09-05T12:00:00+00:00",
        "error": None,
        "messages": [],
        "items": [],
        "approvals": [],
        "capabilities": ["messages", "steer", "interrupt", "approvals", "resume"],
    }

    class _Session:
        def public(self):
            return payload

    class _Manager:
        async def capabilities(self):
            return {"provider": "codex", "available": True, "version": "codex-cli 1", "features": [], "error": None}

        def list_sessions(self, *, task_id=None):
            captured["listed_task_id"] = task_id
            return [payload]

        def get_session(self, thread_id):
            captured["get_session"] = thread_id
            return _Session()

        async def start_session(self, **kwargs):
            captured["start"] = kwargs
            return payload

        async def send_message(self, thread_id, text):
            captured["message"] = (thread_id, text)
            return payload

        async def steer(self, thread_id, text):
            captured["steer"] = (thread_id, text)
            return payload

        async def interrupt(self, thread_id):
            captured["interrupt"] = thread_id
            return payload

        async def resolve_approval(self, thread_id, approval_id, decision):
            captured["approval"] = (thread_id, approval_id, decision)
            return payload

    manager = _Manager()
    monkeypatch.setattr(
        "potluck_local.routers.agents.get_managed_agent_manager", lambda: manager
    )
    app = FastAPI()
    app.include_router(router, prefix="/agents")
    client = TestClient(app)

    started = client.post(
        "/agents/managed/sessions",
        json={
            "task_id": task.id,
            "cwd": str(tmp_path),
            "sandbox": "workspace-write",
            "direction": "Start with the UI.",
        },
    )
    assert started.status_code == 201
    start_args = captured["start"]
    assert isinstance(start_args, dict)
    assert start_args["task_id"] == task.id
    assert "Objective: Control Codex from Tasks" in str(start_args["prompt"])
    assert "Build launch flow" in str(start_args["prompt"])
    assert "Start with the UI." in str(start_args["prompt"])

    assert client.get(f"/agents/managed/sessions?task_id={task.id}").status_code == 200
    assert client.get("/agents/managed/sessions/thread-1").status_code == 200
    assert client.post(
        "/agents/managed/sessions/thread-1/messages", json={"text": " Continue "}
    ).status_code == 200
    assert client.post(
        "/agents/managed/sessions/thread-1/steer", json={"text": "Check tests"}
    ).status_code == 200
    assert client.post("/agents/managed/sessions/thread-1/interrupt").status_code == 200
    assert client.post(
        "/agents/managed/sessions/thread-1/approvals/approval-1",
        json={"decision": "accept"},
    ).status_code == 200
    assert captured["message"] == ("thread-1", "Continue")
    assert captured["steer"] == ("thread-1", "Check tests")
    assert captured["interrupt"] == "thread-1"
    assert captured["approval"] == ("thread-1", "approval-1", "accept")


def test_bridge_fails_open_when_database_is_missing(tmp_path) -> None:
    assert not ingest_payload(
        "codex",
        {"session_id": "s", "hook_event_name": "SessionStart"},
        tmp_path / "missing.db",
    )


def test_connector_merge_and_disconnect_preserve_existing_claude_config(tmp_path) -> None:
    config_path = tmp_path / ".claude" / "settings.local.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo existing"}],
                        }
                    ],
                    "ModelSwitch": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python -m potluck_local.agent_bridge claude",
                                }
                            ]
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    connected = connect_provider(str(tmp_path), "claude")
    merged = json.loads(config_path.read_text(encoding="utf-8"))

    assert connected["state"] == "installed"
    assert connected["missing_events"] == []
    assert merged["permissions"] == {"allow": ["Read"]}
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo existing"
    assert len(merged["hooks"]["PreToolUse"]) == 2
    assert "ModelSwitch" not in merged["hooks"]
    assert "PreModelSwitch" in merged["hooks"]
    assert "PostModelSwitch" in merged["hooks"]

    disconnected = disconnect_provider(str(tmp_path), "claude")
    cleaned = json.loads(config_path.read_text(encoding="utf-8"))

    assert disconnected["state"] == "not-installed"
    assert cleaned["permissions"] == {"allow": ["Read"]}
    assert cleaned["hooks"] == {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo existing"}],
            }
        ]
    }


def test_connector_refuses_invalid_json_without_overwriting(tmp_path) -> None:
    config_path = tmp_path / ".codex" / "hooks.json"
    config_path.parent.mkdir()
    config_path.write_text("{ invalid", encoding="utf-8")

    status = connector_status(str(tmp_path), "codex")
    assert status["state"] == "invalid"
    assert "valid JSON" in status["error"]

    with pytest.raises(AgentConnectorError, match="valid JSON"):
        connect_provider(str(tmp_path), "codex")
    assert config_path.read_text(encoding="utf-8") == "{ invalid"


def test_connector_api_previews_then_installs_codex_hooks(tmp_path) -> None:
    app = FastAPI()
    app.include_router(router, prefix="/agents")
    client = TestClient(app)

    preview = client.get(
        "/agents/connectors/codex", params={"project_path": str(tmp_path)}
    )
    assert preview.status_code == 200
    assert preview.json()["state"] == "not-installed"
    assert preview.json()["config_exists"] is False

    connected = client.post(
        "/agents/connectors/codex/connect", json={"project_path": str(tmp_path)}
    )
    assert connected.status_code == 200
    assert connected.json()["state"] == "installed"
    assert (tmp_path / ".codex" / "hooks.json").is_file()


def test_packaged_mcp_entrypoint_dispatches_to_bridge(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    monkeypatch.setattr(
        "sys.stdin",
        StringIO(
            json.dumps(
                {
                    "session_id": "codex-packaged-session",
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                    "cwd": str(tmp_path),
                }
            )
        ),
    )

    from potluck_local.mcp import server as mcp_server

    mcp_server.main(["--agent-bridge", "codex"])

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT provider, provider_session_id FROM agent_runs"
        ).fetchone()
    assert row == ("codex", "codex-packaged-session")


def test_trace_export_defaults_to_sanitized_metadata(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("POTLUCK_MEMORY_DB_PATH", str(db_path))
    _init(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO mcp_sessions
                (id, transport, started_at, last_seen_at)
            VALUES ('session-1', 'stdio', '2026-09-03T12:00:00+00:00',
                    '2026-09-03T12:00:01+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO mcp_tool_calls
                (id, session_id, tool_name, args_summary, called_at)
            VALUES ('call-1', 'session-1', 'memory_search',
                    '{"query":"PRIVATE SEARCH"}', '2026-09-03T12:00:01+00:00')
            """
        )
        conn.commit()

    app = FastAPI()
    app.include_router(router, prefix="/agents")
    client = TestClient(app)

    sanitized = client.get("/agents/sessions/session-1/trace").json()
    assert sanitized["mode"] == "sanitized"
    assert sanitized["privacy"]["memory_previews_included"] is False
    assert "args_summary" not in sanitized["tool_calls"][0]
    assert "PRIVATE SEARCH" not in json.dumps(sanitized)

    diagnostic = client.get(
        "/agents/sessions/session-1/trace", params={"mode": "local-diagnostic"}
    ).json()
    assert diagnostic["privacy"]["memory_previews_included"] is True
    assert diagnostic["tool_calls"][0]["args_summary"]["query"] == "PRIVATE SEARCH"
