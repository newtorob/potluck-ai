# Potluck MCP on Windows: setup for Claude Code and Claude Desktop

This guide takes you to the point where Claude Code (or Claude Desktop) on Windows can save and recall memories through Potluck. It covers two ways to get `potluck-mcp.exe`: use the copy that the Windows installer includes, or build it from a clone of the repository. No prior Potluck context is required.

> **Status:** `potluck-mcp.exe` ships inside the Windows installer and also builds from a clone on Windows 11. The binary is not code-signed, so Windows SmartScreen can warn on first launch (see *Troubleshooting*).

## What you get

Potluck is a persistent memory and task layer for coding agents. After setup, your Claude Code (and/or Cursor, Zed, Claude Desktop) has access to these 13 tools:

| Tool | What it does |
|---|---|
| `memory_search` | Ranked search over saved memories: pinned memories first, a few standing preferences, then keyword matches (plus semantic matches where an embedding model is available). Call at session start. |
| `memory_save` | Persist a user preference, decision, project fact, etc. |
| `memory_list` | Browse memories by category / project. |
| `memory_delete` | Permanently delete a memory (and its embedding) by id. |
| `memory_pin` | Pin or unpin a memory by id. A pinned memory is always included in search results. |
| `task_create` | Open a multi-step task that can span sessions. |
| `task_list` | See active / completed tasks (call at session start). |
| `task_get` | Fetch a task by id. |
| `task_update` | Edit task fields. |
| `task_complete_step` | Mark a step done (call as you go, don't batch). |
| `task_add_error` | Log an error you hit inside an active task. |
| `session_set_model` | Tell Potluck which model the current agent is using. |
| `session_register_mcps` | Tell Potluck about other MCP servers this agent has loaded. |

All state lives in a local SQLite database at `%USERPROFILE%\.potluck\data\memory.db`. Nothing leaves your machine.

## Get `potluck-mcp.exe`

### Option 1: use the installed app

The Windows installer places `potluck-mcp.exe` in the `resources\python\potluck-mcp\` folder inside the Potluck AI install directory. Use that path in the registration steps below. Keep the `_internal\` folder beside the exe.

### Option 2: build from a clone

#### Prerequisites

- **Windows 10/11 x64.**
- **Python 3.11+** on PATH (`py --version` should print 3.11 or newer). [Get it from python.org.](https://www.python.org/downloads/windows/)
- **Git for Windows**, for the clone.
- Free disk space for the virtual environment and the built bundle. The build installs `requirements.txt`, which includes the engine dependencies. The MCP bundle itself excludes the heaviest ones (see `potluck-mcp.spec`).

#### Build

From an elevated-or-normal PowerShell at the repo root:

```powershell
cd apps\desktop\python

# Create a venv (3.11 is the tested version)
py -3.11 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

# Build the MCP bundle from the committed spec
.\.venv\Scripts\python.exe -m PyInstaller potluck-mcp.spec --noconfirm
```

Result: `apps\desktop\python\dist\potluck-mcp\potluck-mcp.exe` (plus an `_internal\` folder beside it; both must stay together).

**Smoke-check:** double-click `potluck-mcp.exe`. A console window opens, prints a couple of `INFO` lines about WAL mode + `potluck_mcp_start transport=stdio`, then waits for stdin. Close the window or hit `Ctrl+C`. If it crashes with a Python traceback instead, something is wrong; see *Troubleshooting* below.

## Register the MCP server

You probably only need **one** of the sections below. Pick the agent(s) you actually use. In the examples, replace `<path-to>\potluck-mcp.exe` with the path from the previous step, for example `<repo>\apps\desktop\python\dist\potluck-mcp\potluck-mcp.exe` for a build from a clone.

### Claude Code (CLI in your terminal)

Claude Code can take MCP servers from a few places. The simplest is the CLI command with user scope, which registers Potluck for every project on this machine. Without `--scope user`, `claude mcp add` registers the server for the current project only.

```powershell
claude mcp add --scope user potluck "<path-to>\potluck-mcp.exe"
```

That writes an entry into `%USERPROFILE%\.claude.json` (or the equivalent settings file on your version of Claude Code).

**If you'd rather scope to one project**, drop a `.mcp.json` in that project's root:

```json
{
  "mcpServers": {
    "potluck": {
      "type": "stdio",
      "command": "<path-to>\\potluck-mcp.exe",
      "args": [],
      "env": {}
    }
  }
}
```

Note the **doubled backslashes** in JSON strings (`C:\\Users\\...`). A single backslash inside a JSON string is an escape sequence; you'll get a parse error or a silently broken path otherwise.

Restart Claude Code. Run `claude mcp list` (or `/mcp` inside an interactive session). Potluck should be listed and connected.

### Claude Desktop (the macOS/Windows app)

Open (or create) `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "potluck": {
      "command": "<path-to>\\potluck-mcp.exe"
    }
  }
}
```

Restart Claude Desktop. The tool list (slash menu, hammer/wrench icon, depending on version) should now show 13 Potluck tools (`memory_*`, `task_*`, `session_*`).

> **Cursor / Zed users:** their MCP config formats are very similar (`mcp.json` in your Cursor/Zed config dir). Use the same `command` value. This guide has not been checked against them on Windows; file an issue if you hit something.

## Verify end-to-end

Inside any session of your registered agent:

1. Ask the agent: *"Use memory_save to remember that I prefer pnpm over npm."*
2. Confirm a tool call to `memory_save` runs and returns a memory id.
3. Start a **new** session in the same agent.
4. Ask: *"What package manager do I prefer? Check memory_search first."*
5. The agent should call `memory_search`, get the row back, and answer "pnpm."

If step 5 works, Potluck is wired up correctly.

You can also inspect the DB directly:

```powershell
# Requires sqlite3 on PATH; if you don't have it, skip. The agent can list memories with memory_list.
sqlite3 "$env:USERPROFILE\.potluck\data\memory.db" "SELECT id, category, content FROM memories ORDER BY created_at DESC LIMIT 5;"
```

## Troubleshooting

### SmartScreen blocks the first launch

The executable is not code-signed. If Windows SmartScreen shows "Windows protected your PC" on first run, click **More info** then **Run anyway**. A rebuilt exe can trigger it again.

### `ModuleNotFoundError: No module named 'X'` on first run

The committed spec is known-good against the in-repo `requirements.txt`. If you hit this:
1. Verify you installed `requirements.txt` into the same venv you ran PyInstaller from.
2. If `X` is `numpy` specifically: someone re-added numpy to the spec's `excludes` list. Don't do that, because `memory_retrieval.py` and `embeddings.py` import it at module level.
3. If `X` is `backports` or `jaraco`: the `collect_all('setuptools')` line in the spec was removed. Put it back.

### Paths with spaces

If the exe lives somewhere with spaces in the path (e.g. under `C:\Users\My Name\...`), the JSON config string still escapes backslashes the same way (`C:\\Users\\My Name\\...`). The space itself doesn't need escaping inside the JSON string.

### Defender / antivirus quarantines the exe

PyInstaller-built binaries occasionally trip heuristic scanners. If `potluck-mcp.exe` disappears after a build, check Windows Security → Virus & threat protection → Protection history. Restore it and add an exclusion for the `dist\potluck-mcp\` folder.

### MCP server connects but no tools appear

Check the agent's MCP log. The most common cause is that the exe started but exited before the handshake, usually because of a missing DLL. Run the exe by double-clicking it; the console window will show the actual error.

### Server starts but `memory_search` returns 0 hits even after a save

Check that all agents pointing at this binary share the same DB. Potluck pins the DB to `%USERPROFILE%\.potluck\data\memory.db` regardless of where the exe lives. If you set the `POTLUCK_MEMORY_DB_PATH` environment variable for one agent and not another, they read different databases.

## Not covered here

- **Updates.** An exe you build yourself is not updated; rebuild it after pulling changes.
- **`potluck-local.exe` (the full engine with local inference).** It ships in the installer. How its GPU backend is chosen and how the installer is built is in [windows-gpu-inference.md](windows-gpu-inference.md).
- **`potluck-mesh.exe` (the mesh service).** It ships in the installer and is outside the MCP setup path.

## Reporting issues

If something breaks, please open a GitHub issue with: your Windows version (`winver`), Python version (`py --version`), the PyInstaller version (`pip show pyinstaller`), and the first ~20 lines of stderr from running `potluck-mcp.exe` directly.
