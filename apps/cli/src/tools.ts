/**
 * Tool implementations for the agent loop.
 *
 * read_file / list_dir are read-only and auto-run. write_file / run_command are
 * side-effecting and MUST pass through an injectable confirm() gate (the default
 * is a y/N prompt; tests stub it). All paths resolve under the working directory
 * and any path that escapes it is refused.
 */

import { spawn } from "node:child_process";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve } from "node:path";

import { unifiedDiff } from "./diff.js";

export interface ConfirmContext {
  tool: "write_file" | "run_command";
  /** Human-readable body to show before asking — a diff, or the command. */
  summary: string;
  /** The target path (write_file) or the command (run_command). */
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
  "write_file",
  "run_command",
] as const;

/** A recoverable tool failure — surfaced back to the model as TOOL_RESULT. */
class ToolError extends Error {}

const MAX_FILE_BYTES = 64_000;
const MAX_CMD_OUTPUT = 16_000;

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
      case "write_file":
        return await writeFileTool(args, deps);
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
