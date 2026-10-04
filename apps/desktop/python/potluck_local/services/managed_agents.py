"""Local control plane for Potluck-managed coding-agent sessions.

The first adapter speaks Codex App Server's JSONL protocol over stdio. Codex
owns authentication, sandboxing, command execution, and file changes. Potluck
owns the task link, the in-memory workroom projection, user approvals, and the
metadata-only trace written beside observe-only hook runs.

Prompts, responses, commands, tool arguments, and tool results are never
written to Potluck's database or logs. They exist only in this process so the
local workroom can render and control the active session.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from potluck_local.agent_bridge import (
    NormalizedEvent,
    ResourceRef,
    run_id_for,
    write_event,
)

ManagedStatus = Literal[
    "starting",
    "running",
    "waiting_approval",
    "idle",
    "interrupted",
    "failed",
    "stopped",
]
SandboxMode = Literal["read-only", "workspace-write"]
ApprovalDecision = Literal["accept", "acceptForSession", "decline", "cancel"]

_MAX_MESSAGES = 200
_MAX_ITEMS = 300
_MAX_TEXT_CHARS = 200_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean[:limit] if clean else None


def validate_working_directory(value: str) -> Path:
    """Resolve an existing absolute directory without accepting shell syntax."""
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ManagedAgentError("Choose an absolute repository folder.")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ManagedAgentError("The selected repository folder does not exist.") from exc
    if not resolved.is_dir():
        raise ManagedAgentError("The selected repository path is not a folder.")
    return resolved


class ManagedAgentError(RuntimeError):
    """Safe error returned to the local workroom API."""


@dataclass
class ManagedMessage:
    id: str
    role: Literal["user", "assistant"]
    text: str
    status: Literal["streaming", "complete"]
    created_at: str
    kind: Literal["message", "steer"] = "message"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "kind": self.kind,
        }


@dataclass
class ManagedItem:
    id: str
    item_type: str
    label: str
    status: str
    started_at: str
    completed_at: str | None = None
    duration_ms: int | None = None
    detail: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "item_type": self.item_type,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


@dataclass
class PendingApproval:
    id: str
    rpc_id: str | int
    kind: Literal["command", "file_change"]
    title: str
    thread_id: str
    turn_id: str
    item_id: str
    requested_at: str
    command: str | None = None
    cwd: str | None = None
    reason: str | None = None
    available_decisions: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "requested_at": self.requested_at,
            "command": self.command,
            "cwd": self.cwd,
            "reason": self.reason,
            "available_decisions": self.available_decisions,
        }


@dataclass
class ManagedSession:
    thread_id: str
    task_id: str
    task_name: str
    cwd: str
    project: str
    model: str | None
    sandbox: SandboxMode
    status: ManagedStatus
    started_at: str
    updated_at: str
    active_turn_id: str | None = None
    error: str | None = None
    messages: list[ManagedMessage] = field(default_factory=list)
    items: list[ManagedItem] = field(default_factory=list)
    approvals: dict[str, PendingApproval] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return run_id_for("codex", self.thread_id)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.thread_id,
            "run_id": self.run_id,
            "provider": "codex",
            "control_mode": "managed",
            "task_id": self.task_id,
            "task_name": self.task_name,
            "cwd": self.cwd,
            "project": self.project,
            "model": self.model,
            "sandbox": self.sandbox,
            "status": self.status,
            "active_turn_id": self.active_turn_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "messages": [message.public() for message in self.messages],
            "items": [item.public() for item in self.items],
            "approvals": [approval.public() for approval in self.approvals.values()],
            "capabilities": ["messages", "steer", "interrupt", "approvals", "resume"],
        }


class CodexManagedAgentManager:
    """Supervise one local Codex App Server and its managed threads."""

    def __init__(self, *, codex_command: str = "codex") -> None:
        self._codex_command = codex_command
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: dict[str, ManagedSession] = {}
        self._request_id = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._shutting_down = False
        self._version: str | None = None

    async def capabilities(self) -> dict[str, Any]:
        executable = shutil.which(self._codex_command)
        if executable is None:
            return {
                "provider": "codex",
                "available": False,
                "version": None,
                "features": [],
                "error": "Codex CLI was not found on this machine.",
            }
        if self._version is None:
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
                self._version = stdout.decode("utf-8", errors="replace").strip()[:100] or None
            except (OSError, TimeoutError):
                self._version = None
        return {
            "provider": "codex",
            "available": True,
            "version": self._version,
            "features": ["messages", "steer", "interrupt", "approvals", "resume"],
            "error": None,
        }

    def list_sessions(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        sessions = self._sessions.values()
        if task_id is not None:
            sessions = (session for session in sessions if session.task_id == task_id)
        return [
            session.public()
            for session in sorted(sessions, key=lambda item: item.updated_at, reverse=True)
        ]

    def get_session(self, thread_id: str) -> ManagedSession:
        session = self._sessions.get(thread_id)
        if session is None:
            raise ManagedAgentError("Managed session not found in this Potluck process.")
        return session

    async def start_session(
        self,
        *,
        task_id: str,
        task_name: str,
        cwd: str,
        project: str | None,
        prompt: str,
        model: str | None,
        sandbox: SandboxMode,
    ) -> dict[str, Any]:
        workdir = validate_working_directory(cwd)
        await self._ensure_started()

        params: dict[str, Any] = {
            "cwd": str(workdir),
            "sandbox": sandbox,
            "approvalPolicy": "on-request",
            "threadSource": "potluck",
            "serviceName": "potluck",
        }
        if model:
            params["model"] = model
        result = await self._rpc("thread/start", params, timeout=30)
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise ManagedAgentError("Codex did not return a thread identifier.")

        thread_id = thread["id"]
        now = _now_iso()
        session = ManagedSession(
            thread_id=thread_id,
            task_id=task_id,
            task_name=task_name,
            cwd=str(workdir),
            project=project or workdir.name,
            model=_short(result.get("model"), 200) or model,
            sandbox=sandbox,
            status="starting",
            started_at=now,
            updated_at=now,
        )
        self._sessions[thread_id] = session
        session.messages.append(
            ManagedMessage(
                id=f"user-{now}",
                role="user",
                text=prompt[:_MAX_TEXT_CHARS],
                status="complete",
                created_at=now,
            )
        )
        await self._record(
            session,
            event_type="run.started",
            provider_event_name="thread/start",
            status="started",
            correlation_id=thread_id,
            resources=(ResourceRef(kind="task", ref=task_id, action="read"),),
        )

        try:
            turn_result = await self._rpc(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "turnTrigger": "potluck-task",
                },
                timeout=30,
            )
        except Exception as exc:
            session.status = "failed"
            session.error = str(exc)
            session.touch()
            await self._record(
                session,
                event_type="run.failed",
                provider_event_name="turn/start",
                status="failed",
                correlation_id=thread_id,
            )
            raise

        turn = turn_result.get("turn")
        if isinstance(turn, dict):
            session.active_turn_id = _short(turn.get("id"), 500)
        session.status = "running"
        session.touch()
        return session.public()

    async def send_message(self, thread_id: str, text: str) -> dict[str, Any]:
        session = self.get_session(thread_id)
        if session.status in {"starting", "running", "waiting_approval"}:
            raise ManagedAgentError("This session is already working. Use Steer for new guidance.")
        if session.status == "stopped":
            raise ManagedAgentError("This managed session stopped with Potluck.")

        now = _now_iso()
        session.messages.append(
            ManagedMessage(
                id=f"user-{now}",
                role="user",
                text=text[:_MAX_TEXT_CHARS],
                status="complete",
                created_at=now,
            )
        )
        self._trim_session(session)
        session.status = "starting"
        session.error = None
        session.touch()
        await self._record(
            session,
            event_type="run.resumed",
            provider_event_name="turn/start",
            status="started",
            correlation_id=f"resume-{now}",
        )
        result = await self._rpc(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "turnTrigger": "potluck-follow-up",
            },
            timeout=30,
        )
        turn = result.get("turn")
        session.active_turn_id = _short(turn.get("id"), 500) if isinstance(turn, dict) else None
        session.status = "running"
        session.touch()
        return session.public()

    async def steer(self, thread_id: str, text: str) -> dict[str, Any]:
        session = self.get_session(thread_id)
        if session.active_turn_id is None or session.status not in {"running", "waiting_approval"}:
            raise ManagedAgentError("There is no active turn to steer.")
        await self._rpc(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": session.active_turn_id,
                "input": [{"type": "text", "text": text}],
            },
            timeout=15,
        )
        now = _now_iso()
        session.messages.append(
            ManagedMessage(
                id=f"steer-{now}",
                role="user",
                text=text[:_MAX_TEXT_CHARS],
                status="complete",
                created_at=now,
                kind="steer",
            )
        )
        self._trim_session(session)
        session.touch()
        return session.public()

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        session = self.get_session(thread_id)
        if session.active_turn_id is None:
            raise ManagedAgentError("There is no active turn to interrupt.")
        await self._rpc(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": session.active_turn_id},
            timeout=15,
        )
        return session.public()

    async def resolve_approval(
        self,
        thread_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> dict[str, Any]:
        session = self.get_session(thread_id)
        approval = session.approvals.get(approval_id)
        if approval is None:
            raise ManagedAgentError("This approval request is no longer pending.")
        if approval.available_decisions and decision not in approval.available_decisions:
            raise ManagedAgentError("Codex did not offer that approval decision.")

        await self._send({"id": approval.rpc_id, "result": {"decision": decision}})
        session.approvals.pop(approval_id, None)
        session.status = "waiting_approval" if session.approvals else "running"
        session.touch()
        await self._record(
            session,
            event_type="approval.resolved",
            provider_event_name="approval/response",
            status="completed" if decision.startswith("accept") else "declined",
            correlation_id=approval.item_id,
            turn_id=approval.turn_id,
            metadata={"decision": decision, "approval_kind": approval.kind},
        )
        return session.public()

    async def shutdown(self) -> None:
        self._shutting_down = True
        active = [
            session
            for session in self._sessions.values()
            if session.status in {"starting", "running", "waiting_approval"}
        ]
        for session in active:
            session.status = "stopped"
            session.touch()
            await self._record(
                session,
                event_type="run.interrupted",
                provider_event_name="potluck/shutdown",
                status="stopped",
                correlation_id=session.active_turn_id,
                turn_id=session.active_turn_id,
            )

        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()

        tasks = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ManagedAgentError("Codex App Server stopped."))
        self._pending.clear()

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            executable = shutil.which(self._codex_command)
            if executable is None:
                raise ManagedAgentError("Codex CLI was not found on this machine.")
            self._shutting_down = False
            try:
                self._process = await asyncio.create_subprocess_exec(
                    executable,
                    "app-server",
                    "--stdio",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=16 * 1024 * 1024,
                )
            except OSError as exc:
                raise ManagedAgentError("Potluck could not start Codex App Server.") from exc
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "potluck",
                            "title": "Potluck",
                            "version": "0.1.0",
                        }
                    },
                    timeout=15,
                )
                await self._send({"method": "initialized", "params": {}})
            except Exception:
                await self.shutdown()
                raise

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        await self._ensure_started()
        return await self._request(method, params, timeout=timeout)

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"method": method, "id": request_id, "params": params})
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise ManagedAgentError(f"Codex timed out while handling {method}.") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ManagedAgentError("Codex App Server is not running.")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise ManagedAgentError("Codex App Server disconnected.") from exc

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    await self._handle_server_request(message)
                    continue
                if "method" in message:
                    await self._handle_notification(message)
                    continue
                response_id = message.get("id")
                if isinstance(response_id, int):
                    future = self._pending.get(response_id)
                    if future is not None and not future.done():
                        error = message.get("error")
                        if isinstance(error, dict):
                            detail = _short(error.get("message"), 1000) or "Codex request failed."
                            future.set_exception(ManagedAgentError(detail))
                        else:
                            result = message.get("result")
                            future.set_result(result if isinstance(result, dict) else {})
        except asyncio.CancelledError:
            return
        finally:
            if not self._shutting_down:
                await self._mark_process_failed()

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while await process.stderr.readline():
                # Stderr may contain provider or tool details. Drain it so the
                # subprocess cannot block, but never retain or log it.
                pass
        except asyncio.CancelledError:
            return

    async def _mark_process_failed(self) -> None:
        error = ManagedAgentError("Codex App Server stopped unexpectedly.")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for session in self._sessions.values():
            if session.status not in {"idle", "interrupted", "failed", "stopped"}:
                session.status = "failed"
                session.error = str(error)
                session.touch()
                await self._record(
                    session,
                    event_type="run.failed",
                    provider_event_name="app-server/exited",
                    status="failed",
                    correlation_id=session.active_turn_id,
                    turn_id=session.active_turn_id,
                )

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        rpc_id = message.get("id")
        if not isinstance(method, str) or not isinstance(params, dict) or not isinstance(rpc_id, (str, int)):
            return
        thread_id = _short(params.get("threadId"), 500)
        session = self._sessions.get(thread_id or "")
        if session is None:
            await self._send(
                {"id": rpc_id, "error": {"code": -32602, "message": "Unknown managed thread"}}
            )
            return

        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            await self._send(
                {"id": rpc_id, "error": {"code": -32601, "message": "Unsupported request"}}
            )
            return

        item_id = _short(params.get("itemId"), 500) or f"approval-{rpc_id}"
        turn_id = _short(params.get("turnId"), 500) or session.active_turn_id or ""
        approval_id = str(rpc_id)
        command = params.get("command") if isinstance(params.get("command"), str) else None
        available = params.get("availableDecisions")
        decisions = [item for item in available if isinstance(item, str)] if isinstance(available, list) else []
        kind: Literal["command", "file_change"] = (
            "command" if method == "item/commandExecution/requestApproval" else "file_change"
        )
        approval = PendingApproval(
            id=approval_id,
            rpc_id=rpc_id,
            kind=kind,
            title="Run command" if kind == "command" else "Apply file changes",
            thread_id=session.thread_id,
            turn_id=turn_id,
            item_id=item_id,
            requested_at=_now_iso(),
            command=command,
            cwd=_short(params.get("cwd"), 2000),
            reason=_short(params.get("reason"), 2000),
            available_decisions=decisions,
        )
        session.approvals[approval_id] = approval
        session.status = "waiting_approval"
        session.touch()
        await self._record(
            session,
            event_type="approval.requested",
            provider_event_name=method,
            status="waiting",
            correlation_id=item_id,
            turn_id=turn_id,
            metadata={"approval_kind": kind},
        )

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_id = _short(params.get("threadId"), 500)
        session = self._sessions.get(thread_id or "")
        if session is None:
            return

        if method == "turn/started":
            turn = params.get("turn")
            turn_id = _short(turn.get("id"), 500) if isinstance(turn, dict) else None
            session.active_turn_id = turn_id
            session.status = "running"
            session.touch()
            await self._record(
                session,
                event_type="turn.started",
                provider_event_name=method,
                status="started",
                correlation_id=turn_id,
                turn_id=turn_id,
            )
            return

        if method == "item/agentMessage/delta":
            item_id = _short(params.get("itemId"), 500)
            delta = params.get("delta")
            if item_id is not None and isinstance(delta, str):
                message_item = self._message_by_id(session, item_id)
                if message_item is None:
                    message_item = ManagedMessage(
                        id=item_id,
                        role="assistant",
                        text="",
                        status="streaming",
                        created_at=_now_iso(),
                    )
                    session.messages.append(message_item)
                if len(message_item.text) < _MAX_TEXT_CHARS:
                    remaining = _MAX_TEXT_CHARS - len(message_item.text)
                    message_item.text += delta[:remaining]
                session.touch()
                self._trim_session(session)
            return

        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                await self._handle_item(session, method, params, item)
            return

        if method == "turn/completed":
            await self._handle_turn_completed(session, params)
            return

        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            if isinstance(request_id, (str, int)):
                session.approvals.pop(str(request_id), None)
                if not session.approvals and session.status == "waiting_approval":
                    session.status = "running"
                session.touch()

    async def _handle_item(
        self,
        session: ManagedSession,
        method: str,
        params: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        item_id = _short(item.get("id"), 500)
        item_type = _short(item.get("type"), 100)
        if item_id is None or item_type is None:
            return
        turn_id = _short(params.get("turnId"), 500) or session.active_turn_id
        completed = method == "item/completed"

        if item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                message_item = self._message_by_id(session, item_id)
                if message_item is None:
                    message_item = ManagedMessage(
                        id=item_id,
                        role="assistant",
                        text=text[:_MAX_TEXT_CHARS],
                        status="complete" if completed else "streaming",
                        created_at=_now_iso(),
                    )
                    session.messages.append(message_item)
                elif completed:
                    message_item.text = text[:_MAX_TEXT_CHARS]
                    message_item.status = "complete"
            session.touch()
            self._trim_session(session)
            if completed:
                await self._record(
                    session,
                    event_type="message.completed",
                    provider_event_name=method,
                    status="completed",
                    correlation_id=item_id,
                    turn_id=turn_id,
                    metadata={"item_type": item_type},
                )
            return

        if item_type == "userMessage":
            return

        label, detail, tool_name, resources = self._item_projection(session, item)
        display_item = self._item_by_id(session, item_id)
        if display_item is None:
            display_item = ManagedItem(
                id=item_id,
                item_type=item_type,
                label=label,
                status="running" if not completed else self._item_status(item),
                started_at=_now_iso(),
                detail=detail,
            )
            session.items.append(display_item)
        if completed:
            display_item.status = self._item_status(item)
            display_item.completed_at = _now_iso()
            duration = item.get("durationMs")
            display_item.duration_ms = duration if isinstance(duration, int) else None
            if detail is not None:
                display_item.detail = detail
        session.touch()
        self._trim_session(session)

        if item_type == "contextCompaction":
            event_type = "context.compacted" if completed else "context.compacting"
        else:
            failed = display_item.status.lower() in {"failed", "declined", "error"}
            event_type = "tool.failed" if completed and failed else "tool.completed" if completed else "tool.started"
        await self._record(
            session,
            event_type=event_type,
            provider_event_name=method,
            status=display_item.status,
            correlation_id=item_id,
            turn_id=turn_id,
            tool_name=tool_name,
            metadata={"item_type": item_type, "tool_category": self._tool_category(item_type)},
            resources=resources,
        )

        if item_type == "collabAgentToolCall":
            receivers = item.get("receiverThreadIds")
            if isinstance(receivers, list):
                for receiver in receivers:
                    if not isinstance(receiver, str):
                        continue
                    await self._record(
                        session,
                        event_type="agent.ended" if completed else "agent.started",
                        provider_event_name=method,
                        status="completed" if completed else "started",
                        correlation_id=receiver,
                        turn_id=turn_id,
                        provider_agent_id=receiver,
                        agent_kind="subagent",
                        agent_label="Subagent",
                    )

    async def _handle_turn_completed(
        self,
        session: ManagedSession,
        params: dict[str, Any],
    ) -> None:
        turn = params.get("turn")
        turn_id = _short(turn.get("id"), 500) if isinstance(turn, dict) else session.active_turn_id
        provider_status = _short(turn.get("status"), 100) if isinstance(turn, dict) else None
        provider_status = provider_status or "completed"
        session.active_turn_id = None
        session.approvals.clear()
        session.status = (
            "interrupted"
            if provider_status == "interrupted"
            else "failed"
            if provider_status == "failed"
            else "idle"
        )
        if session.status == "failed" and isinstance(turn, dict):
            error = turn.get("error")
            if isinstance(error, dict):
                session.error = _short(error.get("message"), 2000)
        session.touch()
        await self._record(
            session,
            event_type="turn.failed" if session.status == "failed" else "turn.ended",
            provider_event_name="turn/completed",
            status=provider_status,
            correlation_id=turn_id,
            turn_id=turn_id,
        )
        await self._record(
            session,
            event_type=(
                "run.failed"
                if session.status == "failed"
                else "run.interrupted"
                if session.status == "interrupted"
                else "run.ended"
            ),
            provider_event_name="turn/completed",
            status=provider_status,
            correlation_id=turn_id,
            turn_id=turn_id,
        )

    def _item_projection(
        self,
        session: ManagedSession,
        item: dict[str, Any],
    ) -> tuple[str, str | None, str | None, tuple[ResourceRef, ...]]:
        item_type = str(item.get("type") or "activity")
        resources: list[ResourceRef] = []
        if item_type == "commandExecution":
            return "Command", _short(item.get("command"), 50_000), "shell", ()
        if item_type == "fileChange":
            paths: list[str] = []
            changes = item.get("changes")
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    raw_path = change.get("path")
                    if not isinstance(raw_path, str):
                        continue
                    relative = self._relative_path(raw_path, session.cwd)
                    if relative is not None:
                        paths.append(relative)
                        resources.append(ResourceRef(kind="file", ref=relative, action="write"))
            return "File changes", "\n".join(paths) or None, "apply_patch", tuple(resources)
        if item_type == "mcpToolCall":
            server = _short(item.get("server"), 100) or "MCP"
            tool = _short(item.get("tool"), 200) or "tool"
            return f"{server} / {tool}", None, tool, ()
        if item_type == "dynamicToolCall":
            tool = _short(item.get("tool"), 200) or "Tool"
            return tool, None, tool, ()
        if item_type == "collabAgentToolCall":
            tool = _short(item.get("tool"), 100) or "agent"
            return f"Subagent · {tool}", None, tool, ()
        if item_type == "subAgentActivity":
            return "Subagent activity", None, "subagent", ()
        if item_type == "webSearch":
            return "Web search", None, "web_search", ()
        if item_type == "imageView":
            path = _short(item.get("path"), 2000)
            relative = self._relative_path(path, session.cwd) if path else None
            refs = (ResourceRef(kind="file", ref=relative, action="read"),) if relative else ()
            return "View image", relative, "view_image", refs
        if item_type == "contextCompaction":
            return "Context compacted", None, None, ()
        if item_type == "reasoning":
            return "Reasoning", None, None, ()
        return item_type.replace("_", " "), None, item_type, ()

    @staticmethod
    def _relative_path(value: str, cwd: str) -> str | None:
        try:
            resolved = Path(value).resolve() if Path(value).is_absolute() else (Path(cwd) / value).resolve()
            relative = resolved.relative_to(Path(cwd).resolve()).as_posix()
        except (OSError, ValueError):
            return None
        return relative[:500] if relative and relative != "." else None

    @staticmethod
    def _item_status(item: dict[str, Any]) -> str:
        value = item.get("status")
        if isinstance(value, str):
            return value
        if item.get("success") is False or item.get("error"):
            return "failed"
        return "completed"

    @staticmethod
    def _tool_category(item_type: str) -> str:
        return {
            "commandExecution": "shell",
            "fileChange": "file",
            "mcpToolCall": "tool",
            "dynamicToolCall": "tool",
            "collabAgentToolCall": "agent",
            "subAgentActivity": "agent",
            "webSearch": "web",
            "imageView": "file",
            "reasoning": "reasoning",
            "contextCompaction": "context",
        }.get(item_type, "tool")

    @staticmethod
    def _message_by_id(session: ManagedSession, item_id: str) -> ManagedMessage | None:
        return next((item for item in reversed(session.messages) if item.id == item_id), None)

    @staticmethod
    def _item_by_id(session: ManagedSession, item_id: str) -> ManagedItem | None:
        return next((item for item in reversed(session.items) if item.id == item_id), None)

    @staticmethod
    def _trim_session(session: ManagedSession) -> None:
        if len(session.messages) > _MAX_MESSAGES:
            del session.messages[: len(session.messages) - _MAX_MESSAGES]
        if len(session.items) > _MAX_ITEMS:
            del session.items[: len(session.items) - _MAX_ITEMS]

    async def _record(
        self,
        session: ManagedSession,
        *,
        event_type: str,
        provider_event_name: str,
        status: str,
        correlation_id: str | None,
        turn_id: str | None = None,
        tool_name: str | None = None,
        metadata: dict[str, str] | None = None,
        resources: tuple[ResourceRef, ...] = (),
        provider_agent_id: str | None = None,
        agent_kind: str = "root",
        agent_label: str = "Root agent",
    ) -> None:
        event = NormalizedEvent(
            provider="codex",
            provider_session_id=session.thread_id,
            provider_event_name=provider_event_name,
            event_type=event_type,
            status=status,
            project=session.project,
            model=session.model,
            tool_name=tool_name,
            turn_id=turn_id,
            correlation_id=correlation_id,
            provider_agent_id=provider_agent_id,
            agent_kind=agent_kind,
            agent_label=agent_label,
            attribution_confidence="exact",
            occurred_at=_now_iso(),
            metadata=metadata or {},
            resources=resources,
        )
        await asyncio.to_thread(
            write_event,
            event,
            capture_profile="managed-local",
            capture_note=(
                "Full lifecycle from a Potluck-managed Codex session. "
                "Prompt, result, command, and file contents stay outside the trace."
            ),
            control_mode="managed",
            task_id=session.task_id,
            cwd=session.cwd,
        )


_manager: CodexManagedAgentManager | None = None


def get_managed_agent_manager() -> CodexManagedAgentManager:
    global _manager
    if _manager is None:
        _manager = CodexManagedAgentManager()
    return _manager

