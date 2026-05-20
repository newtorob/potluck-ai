"""Potluck MCP server entry point.

Runs as a standalone process — separate from the FastAPI sidecar. Both
processes share state through the same SQLite file at ~/.potluck/data/memory.db.
No IPC between them; SQLite handles concurrent access via file locks (WAL mode
is set by the services layer).

Transports:
  --stdio   JSON-RPC over stdin/stdout  (default; used by Claude Code)
  --tcp PORT  SSE over 127.0.0.1:PORT   (used by GUI agents)

Logging always goes to stderr so it does not corrupt the stdio JSON-RPC stream.

Usage:
    python -m potluck_local.mcp --stdio
    python -m potluck_local.mcp --tcp 8322

Observability:
  On startup: opens an AgentSession row in mcp_sessions.
  clientInfo is captured via a notifications/initialized handler registered on
  the low-level MCPServer — it fires after the handshake, when client_params
  is populated on the ServerSession.
  Each tool call is wrapped: args captured (privacy-filtered), duration timed,
  errors recorded.  Session closed on graceful shutdown.
"""

import argparse
import asyncio
import contextvars
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from potluck_local.mcp.sniffers import sniff_client_config
from potluck_local.mcp.tools.memory import register_memory_tools
from potluck_local.mcp.tools.session import register_session_tools
from potluck_local.mcp.tools.task import register_task_tools
from potluck_local.services.agent_observability import get_observer
from potluck_local.services.memory_db import init_memory_db

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_TCP_PORT = 8322
_LOOPBACK = "127.0.0.1"
_HEARTBEAT_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Per-process session context
# ---------------------------------------------------------------------------

# Holds the Potluck session_id for the current MCP process.
# Accessible from tool implementations via get_current_session_id().
_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "potluck_session_id", default=None
)

# Module-level storage for the session_id and heartbeat task so the shutdown
# path can access them from a synchronous context.
_current_session_id: str | None = None
_heartbeat_task: asyncio.Task | None = None

# Set to True after we've successfully identified the client (one-shot).
_client_identified: bool = False


def get_current_session_id() -> str | None:
    """Return the Potluck session_id for this MCP process, or None if not yet set."""
    return _current_session_id


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def _heartbeat_loop(session_id: str) -> None:
    """Touch last_seen_at every _HEARTBEAT_INTERVAL seconds."""
    observer = get_observer()
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            await observer.heartbeat(session_id)
        except Exception:
            logger.debug("observer_heartbeat_error session=%s", session_id)


# ---------------------------------------------------------------------------
# Tool-call wrapper
# ---------------------------------------------------------------------------


def _extract_memory_ids(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
) -> list[str] | None:
    """Pull the memory ids a tool call touched, from args or result shape.

    Pure ids, no content — already privacy-safe. AgentObserver enforces its
    own whitelist (_MEMORY_TOOL_NAMES) as defense-in-depth, so a non-memory
    tool that returns dicts-with-ids won't accidentally write a memory link.

    Shapes by tool:
      memory_save / memory_get -> dict with "id"
      memory_search / memory_list -> list of dicts with "id"
      memory_delete -> id is in args["memory_id"]
    """
    try:
        if tool_name == "memory_delete":
            mid = args.get("memory_id")
            return [mid] if isinstance(mid, str) and mid else None

        if isinstance(result, dict):
            mid = result.get("id")
            return [mid] if isinstance(mid, str) and mid else None

        if isinstance(result, list):
            ids = [
                item["id"]
                for item in result
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            return ids or None
    except Exception:
        # Never let the observer hook break a tool call.
        return None
    return None


def _wrap_tool_fn(
    tool_name: str,
    fn: Any,
) -> Any:
    """Return an async wrapper that logs the tool call via AgentObserver.

    Also performs lazy client identification on the first tool call: tool
    requests have request_ctx set, so client_params is accessible there.
    Exceptions are always re-raised after logging — never swallowed.
    """
    import functools

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        global _client_identified
        session_id = get_current_session_id()
        observer = get_observer()
        start = time.monotonic()
        error_str: str | None = None
        result_count: int | None = None
        captured_result: Any = None
        try:
            result = await fn(*args, **kwargs)
            captured_result = result
            if isinstance(result, list):
                result_count = len(result)
            return result
        except Exception as exc:
            error_str = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            # Collect args for summarization from kwargs (FastMCP passes by kwarg)
            all_args: dict[str, Any] = dict(kwargs)
            memory_ids = _extract_memory_ids(tool_name, all_args, captured_result)
            if session_id is not None:
                asyncio.create_task(
                    observer.log_tool_call(
                        session_id=session_id,
                        tool_name=tool_name,
                        args=all_args,
                        result_count=result_count,
                        duration_ms=duration_ms,
                        error=error_str,
                        memory_ids_touched=memory_ids,
                    )
                )
                # Update project from args if present
                project = all_args.get("project")
                if isinstance(project, str) and project:
                    asyncio.create_task(
                        observer.update_session(session_id, project=project)
                    )
                # Lazy client identification — runs once on the first tool call
                # where request_ctx IS set (unlike notification handlers).
                if not _client_identified:
                    _client_identified = True  # prevent re-entry before task runs
                    asyncio.create_task(
                        _identify_client_from_request_ctx(session_id)
                    )
            else:
                logger.debug("observer_no_session_for_tool tool=%s", tool_name)

    return wrapper


async def _identify_client_from_request_ctx(session_id: str) -> None:
    """Identify the calling agent from the current request_ctx.

    Called on the first tool dispatch — request_ctx is guaranteed to be set
    at that point (the tool handler runs inside _handle_request which sets it).
    """
    global _client_identified
    import aiosqlite
    from mcp.server.lowlevel.server import request_ctx
    from potluck_local.services.memory_db import get_memory_db_path

    try:
        ctx = request_ctx.get(None)
        if ctx is None:
            _client_identified = False  # allow retry next call
            return
        params = getattr(ctx.session, "client_params", None) or getattr(
            ctx.session, "_client_params", None
        )
        if params is None:
            _client_identified = False
            return
        client_info = params.clientInfo
        client_name: str | None = getattr(client_info, "name", None)
        client_version: str | None = getattr(client_info, "version", None)

        model, peer_mcps = sniff_client_config(client_name or "")
        observer = get_observer()
        await observer.update_session(
            session_id,
            model=model,
            model_source="inferred" if model else None,
            peer_mcps=peer_mcps,
        )
        path = get_memory_db_path()
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE mcp_sessions SET client_name = ?, client_version = ? WHERE id = ?",
                (client_name, client_version, session_id),
            )
            await db.commit()
        logger.info(
            "observer_client_identified session=%s client=%s version=%s model=%s",
            session_id,
            client_name,
            client_version,
            model,
        )
    except Exception:
        _client_identified = False  # allow retry
        logger.exception("observer_identify_client_failed session=%s", session_id)


# ---------------------------------------------------------------------------
# Tool wrappers registered at mcp.add_tool level
# ---------------------------------------------------------------------------

# We wrap tools by monkey-patching FastMCP.add_tool so every tool registered
# through register_*_tools() gets wrapped transparently.

_ORIGINAL_ADD_TOOL = FastMCP.add_tool


def _patched_add_tool(
    self: FastMCP,
    fn: Any,
    name: str | None = None,
    **kwargs: Any,
) -> None:
    effective_name = name or fn.__name__
    wrapped = _wrap_tool_fn(effective_name, fn)
    _ORIGINAL_ADD_TOOL(self, wrapped, name=effective_name, **kwargs)


FastMCP.add_tool = _patched_add_tool  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# FastMCP lifespan — open/close session, start heartbeat
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _observability_lifespan(mcp: FastMCP) -> AsyncIterator[None]:
    """FastMCP lifespan: open an AgentSession and start the heartbeat."""
    global _current_session_id, _heartbeat_task

    transport_str = _resolve_transport()
    observer = get_observer()

    # Open session with what we know before the handshake.
    # clientInfo is captured lazily on the first tool call via request_ctx.
    session_id = await observer.open_session(
        client_name=None,
        client_version=None,
        transport=transport_str,
        pid=os.getpid(),
    )
    _current_session_id = session_id
    _session_id_var.set(session_id)

    # Also register an InitializedNotification handler as best-effort early
    # identification (before any tool calls).
    _register_initialized_handler(mcp._mcp_server, session_id)  # noqa: SLF001

    # Start heartbeat task.
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(session_id))

    try:
        yield
    finally:
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
        await observer.close_session(session_id)
        logger.info("potluck_mcp_observer_shutdown session=%s", session_id)


def _resolve_transport() -> str:
    """Infer transport from sys.argv at startup time."""
    for arg in sys.argv:
        if arg == "--tcp" or arg.startswith("--tcp="):
            return "tcp"
    return "stdio"


def _register_initialized_handler(mcp_server: Any, session_id: str) -> None:
    """Register a notifications/initialized handler to capture clientInfo.

    The MCP SDK sets session._client_params when it processes InitializeRequest
    (in ServerSession._received_request).  The client then sends
    notifications/initialized.  At that point, session._client_params is set
    and accessible.

    Notifications don't have a request_ctx, but they ARE dispatched on the
    same ServerSession instance.  We reach client_params by accessing the
    session stored on the mcp_server after run() starts — using a deferred
    asyncio task so we don't block the notification path.
    """
    import mcp.types as t

    async def on_initialized(notify: t.InitializedNotification) -> None:  # noqa: ARG001
        # Defer slightly to ensure the notification ack completes first.
        asyncio.create_task(_identify_client(session_id, mcp_server))

    mcp_server.notification_handlers[t.InitializedNotification] = on_initialized


async def _identify_client(session_id: str, mcp_server: Any) -> None:
    """Update the session row with clientInfo from the MCP handshake.

    Accesses client_params from the low-level server's request_ctx.  By the
    time notifications/initialized fires, the session is fully initialized and
    the session object is accessible via the server's active connection.

    We use a short retry loop because the ContextVar may not be set in the
    task continuation — the actual client_params is on the ServerSession
    which is the same object used throughout the connection.  We get it by
    looking at the server's _client_params reference stored during ListTools/
    any subsequent request, or by inspecting the existing session via the
    server attribute set up in ServerSession.run().
    """
    import aiosqlite
    from mcp.server.lowlevel.server import request_ctx
    from potluck_local.services.memory_db import get_memory_db_path

    # The notifications/initialized fires before any tool calls, so we may
    # not have a request_ctx yet.  Use a short retry — the first real request
    # will populate it; worst case we update on first tool call via the wrapper.
    params = None
    for _ in range(3):
        try:
            ctx = request_ctx.get(None)
            if ctx is not None:
                params = getattr(ctx.session, "client_params", None) or getattr(
                    ctx.session, "_client_params", None
                )
                if params is not None:
                    break
        except Exception:
            pass
        await asyncio.sleep(0.1)

    # Fallback: check the mcp_server for a stored session reference.
    # The lowlevel server doesn't expose it directly, but we can look at the
    # _mcp_server's request_handlers — if a session ref is stored there we use
    # it.  If we still can't get params, bail gracefully.
    if params is None:
        logger.debug(
            "observer_identify_client_deferred session=%s will retry on first tool call",
            session_id,
        )
        return

    try:
        client_info = params.clientInfo
        client_name: str | None = getattr(client_info, "name", None)
        client_version: str | None = getattr(client_info, "version", None)

        model, peer_mcps = sniff_client_config(client_name or "")
        observer = get_observer()
        await observer.update_session(
            session_id,
            model=model,
            model_source="inferred" if model else None,
            peer_mcps=peer_mcps,
        )
        path = get_memory_db_path()
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE mcp_sessions SET client_name = ?, client_version = ? WHERE id = ?",
                (client_name, client_version, session_id),
            )
            await db.commit()
        logger.info(
            "observer_client_identified session=%s client=%s version=%s model=%s",
            session_id,
            client_name,
            client_version,
            model,
        )
    except Exception:
        logger.exception("observer_identify_client_failed session=%s", session_id)


# ---------------------------------------------------------------------------
# Build MCP
# ---------------------------------------------------------------------------


def _build_mcp(host: str = _LOOPBACK, port: int = _DEFAULT_TCP_PORT) -> FastMCP:
    """Construct and configure the FastMCP instance."""
    mcp = FastMCP(
        name="potluck",
        instructions=(
            "Potluck is the user's persistent memory and task-state layer. "
            "It outlives any single session and stores their preferences, project facts, "
            "decisions, and in-flight work.\n\n"
            "DEFAULT BEHAVIOR — do this without being asked:\n"
            "1. At the start of every new session, call `memory_search` with a query "
            "describing the topic you're about to work on. Always check for relevant "
            "context before answering project, preference, or 'how should I' questions.\n"
            "2. At session start, also call `task_list` (status='active') to see what "
            "the user has in flight. If a relevant task exists, pick up where it left off.\n"
            "3. When the user states a preference, decision, fact about their project, "
            "or hardware/tooling choice, call `memory_save` to persist it. Do not ask "
            "permission for obviously durable facts; do ask before saving anything ambiguous.\n"
            "4. When you start a multi-step task on the user's behalf, call `task_create`. "
            "When you finish a step, call `task_complete_step`.\n\n"
            "Calls cost ~50ms and frequently change the right answer. When in doubt, search."
        ),
        host=host,
        port=port,
        lifespan=_observability_lifespan,
    )
    register_memory_tools(mcp)
    register_task_tools(mcp)
    register_session_tools(mcp)
    return mcp


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="potluck-mcp",
        description="Potluck MCP server — persistent memory and task state for coding agents.",
    )
    transport_group = parser.add_mutually_exclusive_group()
    transport_group.add_argument(
        "--stdio",
        action="store_true",
        default=True,
        help="Use stdio transport (default, for CLI agents like Claude Code).",
    )
    transport_group.add_argument(
        "--tcp",
        metavar="PORT",
        type=int,
        default=None,
        help=f"Use SSE transport on 127.0.0.1:PORT (default port {_DEFAULT_TCP_PORT}).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Called by `potluck-mcp` console script."""
    args = _parse_args(argv)

    # Ensure DB schema exists before accepting any tool calls
    asyncio.run(init_memory_db())

    if args.tcp is not None:
        port = args.tcp
        logger.info("potluck_mcp_start transport=sse host=%s port=%d", _LOOPBACK, port)
        mcp = _build_mcp(host=_LOOPBACK, port=port)
        try:
            mcp.run(transport="sse")
        except KeyboardInterrupt:
            logger.info("potluck_mcp_shutdown transport=sse")
    else:
        logger.info("potluck_mcp_start transport=stdio")
        mcp = _build_mcp()
        try:
            mcp.run(transport="stdio")
        except KeyboardInterrupt:
            logger.info("potluck_mcp_shutdown transport=stdio")


if __name__ == "__main__":
    main()
