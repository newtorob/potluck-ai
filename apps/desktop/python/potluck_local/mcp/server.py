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
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from potluck_local.mcp.tools.memory import register_memory_tools
from potluck_local.mcp.tools.task import register_task_tools
from potluck_local.services.memory_db import init_memory_db

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_TCP_PORT = 8322
_LOOPBACK = "127.0.0.1"


def _build_mcp(host: str = _LOOPBACK, port: int = _DEFAULT_TCP_PORT) -> FastMCP:
    """Construct and configure the FastMCP instance."""
    mcp = FastMCP(
        name="potluck",
        instructions=(
            "Potluck is a local-first persistent memory and task-state layer. "
            "Use memory tools to store and retrieve facts, preferences, decisions, "
            "and context across sessions. Use task tools to track agent workflows."
        ),
        host=host,
        port=port,
    )
    register_memory_tools(mcp)
    register_task_tools(mcp)
    return mcp


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
