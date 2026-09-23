"""Project-local Codex and Claude hook connector management.

The renderer chooses a project directory through Electron's native directory
picker, then calls these helpers through the authenticated local sidecar.  We
never replace a provider config wholesale: only command handlers owned by the
Potluck bridge are added, refreshed, or removed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

Provider = Literal["codex", "claude"]
ConnectorState = Literal["not-installed", "partial", "installed", "invalid", "unavailable"]

CODEX_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
)

CLAUDE_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
    "Notification",
    "TaskCreated",
    "TaskCompleted",
    "PreModelSwitch",
    "PostModelSwitch",
)

_EVENTS: dict[Provider, tuple[str, ...]] = {
    "codex": CODEX_EVENTS,
    "claude": CLAUDE_EVENTS,
}

_CONFIG_PATHS: dict[Provider, Path] = {
    "codex": Path(".codex/hooks.json"),
    "claude": Path(".claude/settings.local.json"),
}

_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class AgentConnectorError(ValueError):
    """A connector request that can be shown directly to the local user."""


def _command_for_args(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def bridge_command(provider: Provider) -> tuple[str, bool]:
    """Return the command the running build can use and whether it exists."""
    if getattr(sys, "frozen", False):
        sidecar = Path(sys.executable).resolve()
        suffix = ".exe" if os.name == "nt" else ""
        binary = sidecar.parent.parent / "potluck-mcp" / f"potluck-mcp{suffix}"
        return _command_for_args([str(binary), "--agent-bridge", provider]), binary.is_file()

    command = _command_for_args(
        [sys.executable, "-m", "potluck_local.agent_bridge", provider]
    )
    available = importlib.util.find_spec("potluck_local.agent_bridge") is not None
    return command, available


def _resolve_config_path(project_path: str, provider: Provider) -> tuple[Path, Path]:
    raw = Path(project_path).expanduser()
    if not raw.is_absolute():
        raise AgentConnectorError("Choose an absolute project folder.")
    try:
        project = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AgentConnectorError("That project folder no longer exists.") from exc
    if not project.is_dir():
        raise AgentConnectorError("Choose a project folder, not a file.")

    relative = _CONFIG_PATHS[provider]
    config_dir = project / relative.parent
    config_path = project / relative
    if config_dir.is_symlink() or config_path.is_symlink():
        raise AgentConnectorError(
            f"Potluck will not edit a symlinked provider config: {config_path}"
        )
    return project, config_path


def _read_config(config_path: Path) -> tuple[dict[str, Any], bool, int | None]:
    if not config_path.exists():
        return {}, False, None
    if not config_path.is_file():
        raise AgentConnectorError(f"Provider config is not a file: {config_path}")
    try:
        file_stat = config_path.stat()
        if file_stat.st_size > _MAX_CONFIG_BYTES:
            raise AgentConnectorError(
                f"Provider config is larger than {_MAX_CONFIG_BYTES // (1024 * 1024)} MB."
            )
        raw = config_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except AgentConnectorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentConnectorError(
            f"Potluck could not read valid JSON from {config_path}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentConnectorError(f"Provider config must contain a JSON object: {config_path}")
    mode = stat.S_IMODE(file_stat.st_mode)
    return parsed, True, mode


def _is_owned_handler(handler: Any, provider: Provider) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    module_marker = f"potluck_local.agent_bridge {provider}"
    binary_marker = f"--agent-bridge {provider}"
    return module_marker in command or binary_marker in command


def _owned_events(config: dict[str, Any], provider: Provider) -> tuple[set[str], set[str]]:
    hooks = config.get("hooks")
    if hooks is None:
        return set(), set()
    if not isinstance(hooks, dict):
        raise AgentConnectorError("The existing top-level hooks value must be a JSON object.")

    current_command, _ = bridge_command(provider)
    installed: set[str] = set()
    stale: set[str] = set()
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise AgentConnectorError(f"The existing hooks.{event} value must be a JSON array.")
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not _is_owned_handler(handler, provider):
                    continue
                if handler.get("command") == current_command:
                    installed.add(str(event))
                else:
                    stale.add(str(event))
    return installed, stale


def _remove_owned_handlers(config: dict[str, Any], provider: Provider) -> int:
    hooks = config.get("hooks")
    if hooks is None:
        return 0
    if not isinstance(hooks, dict):
        raise AgentConnectorError("The existing top-level hooks value must be a JSON object.")

    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            raise AgentConnectorError(f"The existing hooks.{event} value must be a JSON array.")
        kept_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept_handlers = [
                handler for handler in handlers if not _is_owned_handler(handler, provider)
            ]
            removed += len(handlers) - len(kept_handlers)
            if kept_handlers:
                kept_group = deepcopy(group)
                kept_group["hooks"] = kept_handlers
                kept_groups.append(kept_group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        config.pop("hooks", None)
    return removed


def _hook_group(command: str) -> dict[str, Any]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 2,
            }
        ]
    }


def _manual_config(provider: Provider, command: str) -> str:
    hooks = {event: [_hook_group(command)] for event in _EVENTS[provider]}
    return json.dumps({"hooks": hooks}, indent=2) + "\n"


def connector_status(project_path: str, provider: Provider) -> dict[str, Any]:
    project, config_path = _resolve_config_path(project_path, provider)
    command, available = bridge_command(provider)
    expected = set(_EVENTS[provider])
    try:
        config, exists, _ = _read_config(config_path)
        installed, stale = _owned_events(config, provider)
    except AgentConnectorError as exc:
        return {
            "provider": provider,
            "project_path": str(project),
            "config_path": str(config_path),
            "state": "invalid",
            "bridge_available": available,
            "installed_events": [],
            "missing_events": list(_EVENTS[provider]),
            "stale_events": [],
            "planned_connect_events": [],
            "planned_disconnect_events": [],
            "manual_config": _manual_config(provider, command),
            "error": str(exc),
            "config_exists": config_path.exists(),
        }

    expected_installed = expected & installed
    missing = expected - installed
    unexpected_owned = (installed | stale) - expected
    if not available:
        state: ConnectorState = "unavailable"
    elif installed == expected and not stale:
        state = "installed"
    elif installed or stale:
        state = "partial"
    else:
        state = "not-installed"

    return {
        "provider": provider,
        "project_path": str(project),
        "config_path": str(config_path),
        "state": state,
        "bridge_available": available,
        "installed_events": sorted(expected_installed),
        "missing_events": [event for event in _EVENTS[provider] if event in missing],
        "stale_events": sorted(stale | unexpected_owned),
        "planned_connect_events": list(_EVENTS[provider]) if state != "installed" else [],
        "planned_disconnect_events": sorted(installed | stale),
        "manual_config": _manual_config(provider, command),
        "error": None,
        "config_exists": exists,
    }


def _write_config(config_path: Path, config: dict[str, Any], mode: int | None) -> None:
    try:
        config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode if mode is not None else 0o600)
            os.replace(temp_name, config_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise AgentConnectorError(f"Potluck could not update {config_path}: {exc}") from exc


def connect_provider(project_path: str, provider: Provider) -> dict[str, Any]:
    _, config_path = _resolve_config_path(project_path, provider)
    config, _, mode = _read_config(config_path)
    command, available = bridge_command(provider)
    if not available:
        raise AgentConnectorError(
            "The Potluck agent bridge is missing from this build. Rebuild or update Potluck first."
        )

    _remove_owned_handlers(config, provider)
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentConnectorError("The existing top-level hooks value must be a JSON object.")
    for event in _EVENTS[provider]:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise AgentConnectorError(f"The existing hooks.{event} value must be a JSON array.")
        groups.append(_hook_group(command))
    _write_config(config_path, config, mode)
    return connector_status(project_path, provider)


def disconnect_provider(project_path: str, provider: Provider) -> dict[str, Any]:
    _, config_path = _resolve_config_path(project_path, provider)
    config, exists, mode = _read_config(config_path)
    if not exists:
        return connector_status(project_path, provider)
    removed = _remove_owned_handlers(config, provider)
    if removed:
        _write_config(config_path, config, mode)
    return connector_status(project_path, provider)
