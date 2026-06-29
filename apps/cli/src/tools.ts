/**
 * Tool implementations for the agent loop.
 *
 * read_file / list_dir / search_files / glob are read-only and auto-run.
 * write_file / edit_file / run_command are side-effecting and MUST pass through
 * an injectable confirm() gate (the default is a y/N prompt; tests stub it). All
 * paths resolve under the working directory and any path that escapes it is
 * refused.
 */

import { spawn } from "node:child_process";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";

import { unifiedDiff } from "./diff.js";
import {
  escapeRegExp,
  globToRegExp,
  loadIgnore,
  walkFiles,
} from "./walk.js";

export interface ConfirmContext {
  tool: "write_file" | "edit_file" | "run_command";
  /** Human-readable body to show before asking — a diff, or the command. */
  summary: string;
  /** The target path (write_file/edit_file) or the command (run_command). */
  subject: string;
}

export type Confirm = (ctx: ConfirmContext) => Promise<boolean>;

export interface ToolDeps {
  cwd: string;
  confirm: Confirm;
}

export const TOOL_NAMES = [
  "read_file",
  "list_dir",
  "search_files",
  "glob",
  "write_file",
  "edit_file",
  "run_command",
] as const;

/** A recoverable tool failure — surfaced back to the model as TOOL_RESULT. */
class ToolError extends Error {}

const MAX_FILE_BYTES = 64_000;
const MAX_CMD_OUTPUT = 16_000;
const MAX_SEARCH_HITS = 100;
const MAX_GLOB_MATCHES = 200;
const MAX_SEARCH_FILE_BYTES = 2_000_000; // skip very large files when searching

function requireString(
  args: Record<string, unknown>,
  key: string,
): string {
  const value = args[key];
  if (typeof value !== "string") {
    throw new ToolError(`argument "${key}" must be a string`);
  }
  return value;
}

/** Resolve `p` under `cwd`, refusing anything that escapes the directory. */
function resolveInCwd(cwd: string, p: string): string {
  const abs = resolve(cwd, p);
  const rel = relative(cwd, abs);
  if (rel !== "" && (rel === ".." || rel.startsWith(".." + "/") || isAbsolute(rel))) {
    throw new ToolError(`path escapes the working directory: ${p}`);
  }
  return abs;
}

async function readFileTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = requireString(args, "path");
  const abs = resolveInCwd(deps.cwd, path);
  const content = await readFile(abs, "utf8");
  if (content.length > MAX_FILE_BYTES) {
    return `${content.slice(0, MAX_FILE_BYTES)}\n…[truncated at ${MAX_FILE_BYTES} chars]`;
  }
  return content;
}

async function listDirTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = typeof args.path === "string" ? args.path : ".";
  const abs = resolveInCwd(deps.cwd, path);
  const entries = await readdir(abs, { withFileTypes: true });
  if (entries.length === 0) return "(empty directory)";
  return entries
    .map((e) => (e.isDirectory() ? `${e.name}/` : e.name))
    .sort((a, b) => a.localeCompare(b))
    .join("\n");
}

/**
 * search_files — grep-like search across the repo (read-only, auto-runs).
 * `query` is a regex by default; pass `regex: false` for a literal search. An
 * invalid regex falls back to a literal match. Honors .gitignore, skips binary
 * files, and caps at MAX_SEARCH_HITS hits (noting truncation).
 */
async function searchFilesTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const query = requireString(args, "query");
  const sub = typeof args.path === "string" ? args.path : ".";
  const start = resolveInCwd(deps.cwd, sub);
  const asLiteral = args.regex === false;

  let re: RegExp;
  try {
    re = new RegExp(asLiteral ? escapeRegExp(query) : query);
  } catch {
    re = new RegExp(escapeRegExp(query)); // bad regex -> literal fallback
  }

  const rules = await loadIgnore(deps.cwd);
  const hits: string[] = [];
  let truncated = false;

  search: for await (const rel of walkFiles(start, deps.cwd, rules)) {
    const abs = join(deps.cwd, rel);
    let content: string;
    try {
      const buf = await readFile(abs);
      if (buf.length > MAX_SEARCH_FILE_BYTES) continue;
      if (buf.includes(0)) continue; // NUL byte -> treat as binary, skip
      content = buf.toString("utf8");
    } catch {
      continue;
    }
    const lines = content.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (re.test(lines[i])) {
        hits.push(`${rel}:${i + 1}: ${lines[i].trim().slice(0, 200)}`);
        if (hits.length >= MAX_SEARCH_HITS) {
          truncated = true;
          break search;
        }
      }
    }
  }

  if (hits.length === 0) return `no matches for ${query}`;
  const note = truncated ? `\n…[truncated at ${MAX_SEARCH_HITS} matches]` : "";
  return hits.join("\n") + note;
}

/**
 * glob — find files by name pattern (read-only, auto-runs). `**` crosses
 * directories; `*` and `?` stay within a segment. Honors .gitignore and caps at
 * MAX_GLOB_MATCHES (noting truncation).
 */
async function globTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const pattern = requireString(args, "pattern");
  const re = new RegExp(`^${globToRegExp(pattern)}$`);
  const rules = await loadIgnore(deps.cwd);
  const matches: string[] = [];
  let truncated = false;

  for await (const rel of walkFiles(deps.cwd, deps.cwd, rules)) {
    if (re.test(rel)) {
      matches.push(rel);
      if (matches.length >= MAX_GLOB_MATCHES) {
        truncated = true;
        break;
      }
    }
  }

  if (matches.length === 0) return `no files match ${pattern}`;
  const note = truncated ? `\n…[truncated at ${MAX_GLOB_MATCHES} files]` : "";
  return matches.join("\n") + note;
}

async function writeFileTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = requireString(args, "path");
  const content = requireString(args, "content");
  const abs = resolveInCwd(deps.cwd, path);

  let current = "";
  try {
    current = await readFile(abs, "utf8");
  } catch {
    current = ""; // new file
  }

  const summary = unifiedDiff(path, current, content);
  const approved = await deps.confirm({
    tool: "write_file",
    summary,
    subject: path,
  });
  if (!approved) return "user declined";

  await mkdir(dirname(abs), { recursive: true });
  await writeFile(abs, content, "utf8");
  return `wrote ${content.length} bytes to ${path}`;
}

/**
 * edit_file — replace one exact, unique `old_string` with `new_string` in an
 * existing file. This is the preferred way to change existing files; write_file
 * is for creating new ones. Fails clearly if the file is missing, if old_string
 * is absent, or if it appears more than once. Gated behind confirm (shows a diff).
 */
async function editFileTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = requireString(args, "path");
  const oldString = requireString(args, "old_string");
  const newString = requireString(args, "new_string");
  const abs = resolveInCwd(deps.cwd, path);

  if (oldString.length === 0) {
    throw new ToolError("old_string must not be empty (use write_file for new files)");
  }
  if (oldString === newString) {
    throw new ToolError("old_string and new_string are identical — nothing to change");
  }

  let current: string;
  try {
    current = await readFile(abs, "utf8");
  } catch {
    throw new ToolError(
      `file does not exist: ${path} (use write_file to create new files)`,
    );
  }

  const first = current.indexOf(oldString);
  if (first === -1) {
    throw new ToolError(`old_string not found in ${path}`);
  }
  if (current.indexOf(oldString, first + oldString.length) !== -1) {
    throw new ToolError(
      `old_string is not unique in ${path}; add more surrounding context so it matches exactly one place`,
    );
  }

  const updated =
    current.slice(0, first) + newString + current.slice(first + oldString.length);
  const summary = unifiedDiff(path, current, updated);
  const approved = await deps.confirm({ tool: "edit_file", summary, subject: path });
  if (!approved) return "user declined";

  await writeFile(abs, updated, "utf8");
  return `edited ${path}`;
}

function execCommand(
  command: string,
  cwd: string,
): Promise<{ code: number; output: string }> {
  return new Promise((resolveExec) => {
    const child = spawn(command, { cwd, shell: true });
    let output = "";
    let truncated = false;

    const onData = (buf: Buffer): void => {
      if (truncated) return;
      output += buf.toString("utf8");
      if (output.length >= MAX_CMD_OUTPUT) {
        output = output.slice(0, MAX_CMD_OUTPUT);
        truncated = true;
      }
    };

    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (err) => {
      resolveExec({ code: -1, output: `failed to start: ${err.message}` });
    });
    child.on("close", (code) => {
      resolveExec({
        code: code ?? -1,
        output: output + (truncated ? "\n…[output truncated]" : ""),
      });
    });
  });
}

async function runCommandTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const command = requireString(args, "command");
  const approved = await deps.confirm({
    tool: "run_command",
    summary: command,
    subject: command,
  });
  if (!approved) return "user declined";

  const { code, output } = await execCommand(command, deps.cwd);
  const body = output.trim().length > 0 ? output : "(no output)";
  return `exit ${code}\n${body}`;
}

/**
 * Dispatch a tool by name. Recoverable failures (bad args, missing file, escape
 * attempts) are returned as `error: ...` strings so the model can retry; they do
 * not throw.
 */
export async function runTool(
  name: string,
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  try {
    switch (name) {
      case "read_file":
        return await readFileTool(args, deps);
      case "list_dir":
        return await listDirTool(args, deps);
      case "search_files":
        return await searchFilesTool(args, deps);
      case "glob":
        return await globTool(args, deps);
      case "write_file":
        return await writeFileTool(args, deps);
      case "edit_file":
        return await editFileTool(args, deps);
      case "run_command":
        return await runCommandTool(args, deps);
      default:
        return `error: unknown tool "${name}"`;
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return `error: ${message}`;
  }
}
