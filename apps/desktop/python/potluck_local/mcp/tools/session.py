"""MCP session metadata tools — self-reporting model and peer MCPs.

These tools let the calling agent tell Potluck what model is running it and
which other MCP servers it has configured.  They write through AgentObserver
and never touch memory content — no privacy gate needed.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_session_tools(mcp: FastMCP) -> None:
    """Register session metadata tools onto the provided FastMCP instance."""

    @mcp.tool(
        name="session_set_model",
        description=(
            "Tell Potluck what model is powering this agent session. "
            "e.g. session_set_model('claude-opus-4-7'). "
            "Sets model_source='self-reported'. "
            "Call this once early in a session so the Potluck desktop app "
            "can display which model is driving each agent."
        ),
    )
    async def session_set_model(model: str) -> dict:
        """Self-report the model name for this session."""
        from potluck_local.mcp.server import get_current_session_id
        from potluck_local.services.agent_observability import get_observer

        session_id = get_current_session_id()
        if session_id is None:
            logger.warning("session_set_model_no_session model=%s", model)
            return {"ok": False, "reason": "no_active_session"}

        observer = get_observer()
        await observer.update_session(
            session_id,
            model=model,
            model_source="self-reported",
        )
        logger.info("session_model_set session=%s model=%s", session_id, model)
        return {"ok": True, "model": model, "model_source": "self-reported"}

    @mcp.tool(
        name="session_register_mcps",
        description=(
            "Tell Potluck which other MCP servers this agent has configured. "
            "e.g. session_register_mcps(['potluck','gmail','supabase']). "
            "Pure metadata — Potluck never tries to contact these other servers. "
            "Helps the desktop app show a complete picture of agent capabilities."
        ),
    )
    async def session_register_mcps(servers: list[str]) -> dict:
        """Self-report peer MCP servers for this session."""
        from potluck_local.mcp.server import get_current_session_id
        from potluck_local.services.agent_observability import get_observer

        session_id = get_current_session_id()
        if session_id is None:
            logger.warning("session_register_mcps_no_session servers=%s", servers)
            return {"ok": False, "reason": "no_active_session"}

        # Filter out potluck itself
        filtered = [s for s in servers if s.lower() != "potluck"]
        observer = get_observer()
        await observer.update_session(session_id, peer_mcps=filtered)
        logger.info(
            "session_mcps_registered session=%s count=%d",
            session_id,
            len(filtered),
        )
        return {"ok": True, "registered": filtered}
