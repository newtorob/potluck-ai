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

// Verify output is tail-capped (build/type errors cluster at the END), unlike
// run_command which keeps the head. A rolling buffer bounds memory on a runaway
// build; the final result is trimmed to the last few KB / lines.
const VERIFY_KEEP_BYTES = 3_000;
const VERIFY_KEEP_LINES = 40;
const VERIFY_BUFFER_BYTES = 256_000;

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

  // Guard against the classic weak-model mistake: overwriting an existing,
  // non-empty file with empty content. Seen live -- a 3B model blanked a file
  // with a pointless empty write AFTER it had already made the real edit. Refuse
  // hard so --yes can't auto-approve a destructive no-op.
  if (current.trim().length > 0 && content.trim().length === 0) {
    throw new ToolError(
      `refusing to overwrite ${path} (${current.length} bytes) with empty content. ` +
        `Use edit_file for a targeted change instead.`,
    );
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

// ---------------------------------------------------------------------------
// edit_file matching: exact -> trailing-whitespace-flex -> indent-flex
// ---------------------------------------------------------------------------

/** All start offsets of `needle` in `hay`. */
function allIndexes(hay: string, needle: string): number[] {
  const out: number[] = [];
  for (let i = hay.indexOf(needle); i !== -1; i = hay.indexOf(needle, i + 1)) {
    out.push(i);
  }
  return out;
}

/** Char [start,end) span of each line (end excludes the trailing newline). */
function lineSpans(text: string): { start: number; end: number; text: string }[] {
  const spans: { start: number; end: number; text: string }[] = [];
  let pos = 0;
  for (const line of text.split("\n")) {
    spans.push({ start: pos, end: pos + line.length, text: line });
    pos += line.length + 1;
  }
  return spans;
}

const leadingWs = (s: string): string => (/^[ \t]*/.exec(s) ?? [""])[0];

/** Char spans in `current` whose consecutive lines equal `needle` under `norm`. */
function lineWindowSpans(
  current: string,
  needle: string[],
  norm: (s: string) => string,
): { start: number; end: number }[] {
  const hay = lineSpans(current);
  const n = needle.length;
  if (n === 0 || n > hay.length) return [];
  const out: { start: number; end: number }[] = [];
  for (let i = 0; i + n <= hay.length; i++) {
    let ok = true;
    for (let k = 0; k < n; k++) {
      if (norm(hay[i + k].text) !== norm(needle[k])) {
        ok = false;
        break;
      }
    }
    if (ok) out.push({ start: hay[i].start, end: hay[i + n - 1].end });
  }
  return out;
}

/** The longest leading-whitespace prefix shared by all non-blank lines. */
function commonLeadingWs(lines: string[]): string {
  const nonBlank = lines.filter((l) => l.trim().length > 0);
  if (nonBlank.length === 0) return "";
  let prefix = leadingWs(nonBlank[0]);
  for (const l of nonBlank.slice(1)) {
    const lw = leadingWs(l);
    let i = 0;
    while (i < prefix.length && i < lw.length && prefix[i] === lw[i]) i++;
    prefix = prefix.slice(0, i);
    if (prefix === "") break;
  }
  return prefix;
}

/** Strip `indent` from the front of `line` (or all leading ws if it doesn't match). */
function dedentLine(line: string, indent: string): string {
  if (line.trim().length === 0) return "";
  return line.startsWith(indent) ? line.slice(indent.length) : line.replace(/^[ \t]*/, "");
}

/** Re-indent `block` from base `fromIndent` to base `toIndent`, keeping nesting. */
function reindentBlock(block: string, fromIndent: string, toIndent: string): string {
  return block
    .split("\n")
    .map((line) => (line.trim().length === 0 ? line : toIndent + dedentLine(line, fromIndent)))
    .join("\n");
}

interface EditResult {
  updated: string;
  count: number;
  /** true when a whitespace/indent-flexible tier matched (not a byte-exact hit). */
  fuzzy: boolean;
}

function editNotFound(path: string): string {
  return (
    `old_string not found in ${path}. Re-read the file and copy the target text; ` +
    `minor whitespace/indentation differences are tolerated, but the lines must otherwise match.`
  );
}

/**
 * Resolve an edit with an increasing-leniency cascade so weak models' drift on
 * whitespace and indentation still lands the edit on the first try instead of
 * looping: (1) exact substring, (2) per-line trailing-whitespace-insensitive,
 * (3) per-line indent-insensitive (re-indenting new_string to the file's actual
 * indentation). The diff-confirm gate still shows exactly what will change.
 */
function resolveEdit(
  current: string,
  oldString: string,
  newString: string,
  replaceAll: boolean,
  path: string,
): EditResult | { error: string } {
  // Tier 1: exact substring.
  const exact = allIndexes(current, oldString);
  if (exact.length === 1) {
    return {
      updated:
        current.slice(0, exact[0]) + newString + current.slice(exact[0] + oldString.length),
      count: 1,
      fuzzy: false,
    };
  }
  if (exact.length > 1) {
    if (!replaceAll) {
      return {
        error:
          `old_string is not unique in ${path} (appears ${exact.length} times); add more ` +
          `surrounding context, or pass "replace_all": true to change every occurrence.`,
      };
    }
    return {
      updated: current.split(oldString).join(newString),
      count: exact.length,
      fuzzy: false,
    };
  }

  // Fuzzy tiers, line-based. Drop a phantom trailing "" if old_string ended in \n.
  let needle = oldString.split("\n");
  const oldTrailNL = needle.length > 1 && needle[needle.length - 1] === "";
  if (oldTrailNL) needle = needle.slice(0, -1);
  if (needle.length === 0) return { error: editNotFound(path) };

  // The matched span excludes the last line's trailing newline, so drop one from
  // new_string too (the file's existing newline after the block stays in place).
  let repl = newString;
  if (oldTrailNL && repl.endsWith("\n")) repl = repl.slice(0, -1);

  const rtrim = (s: string): string => s.replace(/[ \t\r]+$/, "");
  const ambiguous = (n: number, how: string): { error: string } => ({
    error:
      `old_string matches ${n} places in ${path} (${how}); add more surrounding ` +
      `context, or pass "replace_all": true.`,
  });
  const applySpans = <T extends { start: number; end: number }>(
    spans: T[],
    piece: (span: T) => string,
  ): EditResult => {
    const chosen = replaceAll ? spans : [spans[0]];
    let updated = current;
    for (let s = chosen.length - 1; s >= 0; s--) {
      // Apply last-to-first so earlier offsets stay valid.
      updated = updated.slice(0, chosen[s].start) + piece(chosen[s]) + updated.slice(chosen[s].end);
    }
    return { updated, count: chosen.length, fuzzy: true };
  };

  // Tier 2: trailing-whitespace-insensitive (leading indentation still exact).
  const trailing = lineWindowSpans(current, needle, rtrim);
  if (trailing.length > 1 && !replaceAll) return ambiguous(trailing.length, "trailing whitespace");
  if (trailing.length >= 1) return applySpans(trailing, () => repl);

  // Tier 3: uniform-indent — the whole block written at a consistent wrong
  // indent. We ONLY accept a single uniform offset (reject non-uniform drift
  // rather than silently apply mis-indented code), then re-indent new_string to
  // the file's actual indentation.
  const needleMin = commonLeadingWs(needle);
  const dedentedNeedle = needle.map((l) => dedentLine(l, needleMin));
  const hay = lineSpans(current);
  const indented: { start: number; end: number; hayIndent: string }[] = [];
  for (let i = 0; i + needle.length <= hay.length; i++) {
    const window = hay.slice(i, i + needle.length).map((h) => h.text);
    const hayIndent = commonLeadingWs(window);
    if (window.every((l, k) => dedentLine(l, hayIndent) === dedentedNeedle[k])) {
      indented.push({ start: hay[i].start, end: hay[i + needle.length - 1].end, hayIndent });
    }
  }
  if (indented.length > 1 && !replaceAll) return ambiguous(indented.length, "indentation");
  if (indented.length >= 1) {
    return applySpans(indented, (span) => reindentBlock(repl, needleMin, span.hayIndent));
  }

  return { error: editNotFound(path) };
}

/**
 * edit_file — replace `old_string` with `new_string` in an existing file. The
 * preferred way to change existing files; write_file is for creating new ones.
 * Matching is a cascade (exact -> trailing-whitespace-flex -> indent-flex), so
 * small whitespace drift still lands. Fails clearly if the file is missing, the
 * text is absent, or it is ambiguous. Gated behind confirm (shows a diff).
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

  const replaceAll = args.replace_all === true;
  const result = resolveEdit(current, oldString, newString, replaceAll, path);
  if ("error" in result) throw new ToolError(result.error);

  const summary = unifiedDiff(path, current, result.updated);
  const approved = await deps.confirm({ tool: "edit_file", summary, subject: path });
  if (!approved) return "user declined";

  await writeFile(abs, result.updated, "utf8");
  const parts = [`edited ${path}`];
  if (result.count > 1) parts.push(`(${result.count} occurrences)`);
  if (result.fuzzy) parts.push("(matched with whitespace flexibility)");
  return parts.join(" ");
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

export interface VerifyOutcome {
  code: number;
  /** stdout+stderr, tail-capped to the last few lines / KB. */
  output: string;
}

/** Keep the last VERIFY_KEEP_LINES lines, then the last VERIFY_KEEP_BYTES chars. */
function tailCap(s: string): string {
  let out = s;
  const lines = out.split("\n");
  if (lines.length > VERIFY_KEEP_LINES) out = lines.slice(-VERIFY_KEEP_LINES).join("\n");
  if (out.length > VERIFY_KEEP_BYTES) out = out.slice(out.length - VERIFY_KEEP_BYTES);
  return out.trim();
}

/**
 * Run a FIXED verify command (build/tests) after an edit. Same child_process
 * spawn as run_command, but it is NOT gated (the user configured it) and the
 * captured output is tail-capped — build errors land at the end.
 */
export function runVerify(command: string, cwd: string): Promise<VerifyOutcome> {
  return new Promise((resolveExec) => {
    const child = spawn(command, { cwd, shell: true });
    let buf = "";

    const onData = (b: Buffer): void => {
      buf += b.toString("utf8");
      // Rolling tail buffer so a noisy build can't exhaust memory.
      if (buf.length > VERIFY_BUFFER_BYTES) buf = buf.slice(buf.length - VERIFY_BUFFER_BYTES);
    };

    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (err) => {
      resolveExec({ code: -1, output: `failed to start: ${err.message}` });
    });
    child.on("close", (code) => {
      resolveExec({ code: code ?? -1, output: tailCap(buf) });
    });
  });
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
