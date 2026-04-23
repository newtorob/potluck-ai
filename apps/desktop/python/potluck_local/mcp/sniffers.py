"""Best-effort config-file readers for known MCP client applications.

sniff_client_config(client_name) -> (model, peer_mcps)

Returns (None, []) when the client is unknown, its config cannot be read, or
the relevant keys are absent.  All failures are swallowed — this is advisory
metadata, never load-bearing for tool execution.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def sniff_client_config(client_name: str) -> tuple[str | None, list[str]]:
    """Return (model, peer_mcps) for a known client.

    peer_mcps excludes "potluck" itself.
    Returns (None, []) for unknown clients or unreadable configs.
    """
    if client_name is None:
        return None, []
    name_lower = client_name.lower()
    if "claude" in name_lower:
        return _sniff_claude_code()
    if "cursor" in name_lower:
        return _sniff_cursor()
    if "zed" in name_lower:
        return _sniff_zed()
    if "windsurf" in name_lower:
        return _sniff_windsurf()
    return None, []


def _sniff_claude_code() -> tuple[str | None, list[str]]:
    """Read ~/.claude.json to extract defaultModel and mcpServers keys."""
    config_path = Path.home() / ".claude.json"
    try:
        if not config_path.exists():
            return None, []
        raw = config_path.read_text(encoding="utf-8")
        data: dict = json.loads(raw)
    except Exception as exc:
        logger.debug("sniff_claude_code_read_failed path=%s error=%s", config_path, exc)
        return None, []

    # Model — try common key paths in order of preference.
    model: str | None = None
    for key in ("defaultModel", "model", "selectedModel"):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate:
            model = candidate
            break

    # Try project-scoped defaultModel using cwd as the key.
    if model is None:
        cwd = os.getcwd()
        projects = data.get("projects", {})
        if isinstance(projects, dict):
            project_data = projects.get(cwd, {})
            if isinstance(project_data, dict):
                candidate = project_data.get("defaultModel")
                if isinstance(candidate, str) and candidate:
                    model = candidate

    # Peer MCPs — take mcpServers keys, filter out potluck.
    peer_mcps: list[str] = []
    mcp_servers = data.get("mcpServers")
    if isinstance(mcp_servers, dict):
        peer_mcps = [k for k in mcp_servers.keys() if k.lower() != "potluck"]

    logger.debug(
        "sniff_claude_code model=%s peer_mcps_count=%d", model, len(peer_mcps)
    )
    return model, peer_mcps


def _sniff_cursor() -> tuple[str | None, list[str]]:
    """Stub — read Cursor's config to extract model and MCP server list.

    TODO: Cursor stores its config at:
      macOS: ~/Library/Application Support/Cursor/User/settings.json
      Linux: ~/.config/Cursor/User/settings.json
      Windows: %APPDATA%/Cursor/User/settings.json
    Relevant keys: "cursor.model", "cursor.mcp.servers"
    """
    return None, []


def _sniff_zed() -> tuple[str | None, list[str]]:
    """Stub — read Zed's config to extract model and MCP server list.

    TODO: Zed stores its config at:
      macOS: ~/.config/zed/settings.json
      Linux: ~/.config/zed/settings.json
    Relevant keys: "language_models", "context_servers"
    """
    return None, []


def _sniff_windsurf() -> tuple[str | None, list[str]]:
    """Stub — read Windsurf's config to extract model and MCP server list.

    TODO: Windsurf stores its config at:
      macOS: ~/Library/Application Support/Windsurf/User/settings.json
      Linux: ~/.config/Windsurf/User/settings.json
    Relevant keys: similar to Cursor's VSCode-derived layout.
    """
    return None, []
