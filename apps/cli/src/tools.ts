/**
 * Tool implementations for the agent loop.
 *
 * read_file / list_dir / search_files / glob are read-only and auto-run.
 * write_file / edit_file / run_command are side-effecting and must pass through
 * an injectable confirm() gate (the default is a y/N prompt; tests stub it). All
 * paths resolve under the working directory and any path that escapes it is
 * refused. Containment is checked on the canonical path (symlinks resolved),
 * not the lexical one; see resolveInCwd.
 */

import { commandProcess } from "./command-process.js";
import { createReadStream } from "node:fs";
import {
  lstat,
  mkdir,
  readdir,
  readFile,
  readlink,
  realpath,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import { unifiedDiff } from "./diff.js";
import {
  escapeRegExp,
  globToRegExp,
  ignoredBy,
  loadIgnore,
  walkFiles,
} from "./walk.js";

export interface ConfirmContext {
  tool: "write_file" | "edit_file" | "run_command";
  /** Human-readable body to show before asking - a diff, or the command. */
  summary: string;
  /** The target path (write_file/edit_file) or the command (run_command). */
  subject: string;
}

export type Confirm = (ctx: ConfirmContext) => Promise<boolean>;

export interface ToolDeps {
  signal?: AbortSignal;
  cwd: string;
  confirm: Confirm;
  signal?: AbortSignal;
  /** The approved shell actually started; it may mutate files even if it exits nonzero. */
  onCommandStarted?: () => void;
  /**
   * Absolute paths the agent has fully read this task. read_file adds to it; write_file
   * refuses to blind-overwrite an existing file that isn't in it. Optional so
   * direct tool tests can omit it (the guard is then inert).
   */
  readPaths?: Set<string>;
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

/** A recoverable tool failure - surfaced back to the model as TOOL_RESULT. */
class ToolError extends Error {}

const MAX_READ_CHARS = 16_000;
const DEFAULT_READ_LINES = 200;
const MAX_READ_LINES = 2_000;
const MAX_CMD_OUTPUT = 16_000;
const MAX_SEARCH_HITS = 100;
const MAX_GLOB_MATCHES = 200;
const MAX_SEARCH_FILE_BYTES = 2_000_000; // skip very large files when searching
// Symlink hops canonicalize() will follow by hand before giving up. Bounds a
// cycle (a -> b -> a), which realpath would report as ELOOP.
const MAX_SYMLINK_HOPS = 32;

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

/**
 * Canonicalize `abs` by resolving every symlink along it. When the path does not
 * exist yet (writing a NEW file), canonicalize the nearest existing ancestor and
 * re-append the missing tail, so a new file under a symlinked-outside directory
 * is still judged by where that directory really points.
 *
 * `raw` is the caller's original argument, used only to shape a refusal message;
 * a resolved target is never named in one.
 */
async function canonicalize(abs: string, raw: string): Promise<string> {
  let current = abs;
  const tail: string[] = [];
  let hops = 0;

  for (;;) {
    try {
      const real = await realpath(current);
      return tail.length > 0 ? join(real, ...tail) : real;
    } catch {
      // realpath reports ENOENT for a genuinely missing component AND for a
      // DANGLING symlink, so lstat decides which one this is. Calling a dangling
      // link "missing" judged containment on the link's own in-workspace path
      // while the open that followed created the file at the link's target,
      // outside the workspace. One dangling link is enough, and git stores them,
      // so a cloned repo carried the escape.
      let target: string | null = null;
      try {
        const info = await lstat(current);
        if (info.isSymbolicLink()) target = await readlink(current);
      } catch {
        target = null; // genuinely missing, or unreadable
      }

      if (target !== null) {
        if (++hops > MAX_SYMLINK_HOPS) {
          // A cycle, or a chain too deep to vouch for. Refuse rather than guess.
          throw new ToolError(`path escapes the working directory: ${raw}`);
        }
        // A relative link target is relative to the directory holding the link.
        current = resolve(dirname(current), target);
        continue;
      }

      const parent = dirname(current);
      // No resolvable ancestor all the way up: fall back to the lexical path.
      // The fs call that follows will fail on its own, and the containment check
      // still runs against what we could resolve.
      if (parent === current) return abs;
      tail.unshift(basename(current));
      current = parent;
    }
  }
}

/** True when `target` is the canonical root or sits underneath it. */
function withinRoot(root: string, target: string): boolean {
  // A plain string prefix compare would call /work2 a child of /work, so test
  // the relative path instead: "" is the root itself, ".." or "../x" climbs out
  // of it, and an absolute result means a different volume entirely.
  const rel = relative(root, target);
  if (rel === "") return true;
  return rel !== ".." && !rel.startsWith(".." + sep) && !isAbsolute(rel);
}

/** The workspace root with its own symlinks resolved. */
function canonicalRoot(cwd: string): Promise<string> {
  // The root itself is often a symlink: on macOS /tmp resolves to /private/tmp,
  // so os.tmpdir() workspaces would reject every path inside them if the root
  // were left uncanonicalized.
  return canonicalize(resolve(cwd), cwd);
}

/**
 * Resolve `p` under `cwd`, refusing anything that escapes the directory.
 *
 * The check runs on the canonical path (symlinks resolved). A lexically-inside
 * path can be a symlink (or sit under a symlinked directory) whose target is
 * outside the workspace, and every fs call after this check follows symlinks, so
 * a lexical-only check would let read_file return arbitrary files.
 *
 * Residual race: this is check-then-use. A symlink swapped between this check
 * and the open that follows would still be followed. Closing that needs an
 * O_NOFOLLOW walk of every segment against an open directory handle; accepted as
 * out of scope.
 *
 * Async because realpath is async. It stays module-private; the five call sites
 * simply await it.
 */
async function resolveInCwd(cwd: string, p: string): Promise<string> {
  const root = await canonicalRoot(cwd);
  const abs = await canonicalize(resolve(cwd, p), p);
  if (!withinRoot(root, abs)) {
    throw new ToolError(`path escapes the working directory: ${p}`);
  }
  return abs;
}

function staleApproval(path: string): ToolError {
  return new ToolError(
    `file ${path} changed while awaiting approval. Nothing was written. ` +
    `Re-read the file and request a new change so its current diff can be approved.`,
  );
}

/** The approved diff must still describe the same target and bytes. */
async function checkApprovedFile(
  deps: ToolDeps,
  path: string,
  abs: string,
  expected: Buffer | null,
): Promise<void> {
  try {
    if (await resolveInCwd(deps.cwd, path) !== abs) throw staleApproval(path);
    let current: Buffer | null;
    try {
      current = await readFile(abs);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      current = null;
    }
    if (expected === null ? current !== null : current === null || !current.equals(expected)) {
      throw staleApproval(path);
    }
  } catch {
    deps.readPaths?.delete(abs);
    throw staleApproval(path);
  }
  // This closes the approval wait window; it is not a filesystem transaction.
  // Another process can still write between this check and the subsequent write.
}

async function readFileTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = requireString(args, "path");
  const offset = readPositiveInteger(args, "offset", 1);
  const limit = readPositiveInteger(args, "limit", DEFAULT_READ_LINES, MAX_READ_LINES);
  const abs = await resolveInCwd(deps.cwd, path);
  // A partial inspection cannot establish that a previous full read is current.
  deps.readPaths?.delete(abs);
  const range = await readLineRange(abs, offset, limit);
  if (offset === 1 && range.eof && !range.truncatedLine) {
    deps.readPaths?.add(abs);
    return range.content;
  }
  const next = range.lastLine + 1;
  const guidance = range.truncatedLine
    ? `Line ${range.lastLine} is truncated at the ${MAX_READ_CHARS}-character output limit. ` +
      `Use an approved run_command to inspect a bounded character range within that line. ` +
      `To inspect any later lines, use read_file with offset=${next} and limit=${limit}.`
    : range.eof
      ? "End of file."
      : `Continue with read_file offset=${next} and limit=${limit}.`;
  return `[Partial read: lines ${offset}-${range.lastLine}. ${guidance} ` +
    `Use edit_file for targeted changes; this does not permit a whole-file overwrite.]\n` +
    range.content;
}

function readPositiveInteger(
  args: Record<string, unknown>,
  key: string,
  fallback: number,
  max = Number.MAX_SAFE_INTEGER,
): number {
  const value = args[key];
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1 || value > max) {
    throw new ToolError(`argument "${key}" must be an integer from 1 to ${max}`);
  }
  return value;
}

/** Keep UTF-16 surrogate pairs and CRLF line endings intact at the output cap. */
function readPrefix(text: string, max: number): string {
  let end = Math.min(text.length, max);
  const last = text.charCodeAt(end - 1);
  const next = text.charCodeAt(end);
  if ((last >= 0xd800 && last <= 0xdbff && next >= 0xdc00 && next <= 0xdfff) ||
      (text[end - 1] === "\r" && text[end] === "\n")) {
    end--;
  }
  return text.slice(0, end);
}

interface ReadRange {
  content: string;
  lastLine: number;
  eof: boolean;
  truncatedLine: boolean;
}

/** Stream past unrequested lines without retaining a large file or oversized line. */
async function readLineRange(abs: string, offset: number, limit: number): Promise<ReadRange> {
  let content = "";
  let pending = "";
  let line = 1;
  let lastLine = 0;
  let hasUnterminatedLine = false;
  for await (const chunk of createReadStream(abs, { encoding: "utf8" })) {
    let pos = 0;
    while (pos < chunk.length) {
      if (lastLine >= offset && lastLine - offset + 1 >= limit) {
        return { content, lastLine, eof: false, truncatedLine: false };
      }
      const newline = chunk.indexOf("\n", pos);
      const end = newline === -1 ? chunk.length : newline + 1;
      hasUnterminatedLine = newline === -1;
      if (line >= offset) {
        const remaining = MAX_READ_CHARS - content.length;
        if (pending.length + end - pos > remaining) {
          // Prefer complete lines. Only truncate when one line alone exceeds the cap.
          if (content.length > 0) {
            return { content, lastLine, eof: false, truncatedLine: false };
          }
          pending += chunk.slice(pos, pos + remaining - pending.length + 1);
          return {
            content: readPrefix(pending, remaining),
            lastLine: line,
            eof: false,
            truncatedLine: true,
          };
        }
        pending += chunk.slice(pos, end);
        if (newline !== -1) {
          content += pending;
          pending = "";
          lastLine = line;
        }
      }
      if (newline !== -1) line++;
      pos = end;
    }
  }
  if (pending.length > 0) {
    content += pending;
    lastLine = line;
  }
  const totalLines = hasUnterminatedLine ? line : line - 1;
  if (offset > Math.max(1, totalLines)) {
    throw new ToolError(`offset ${offset} is past end of file (${totalLines} lines)`);
  }
  return { content, lastLine, eof: true, truncatedLine: false };
}

async function listDirTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const path = typeof args.path === "string" ? args.path : ".";
  const abs = await resolveInCwd(deps.cwd, path);
  const entries = await readdir(abs, { withFileTypes: true });
  if (entries.length === 0) return "(empty directory)";
  return entries
    .map((e) => (e.isDirectory() ? `${e.name}/` : e.name))
    .sort((a, b) => a.localeCompare(b))
    .join("\n");
}

/**
 * search_files - grep-like search across the repo (read-only, auto-runs).
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
  const root = await canonicalRoot(deps.cwd);
  const start = await resolveInCwd(deps.cwd, sub);
  const asLiteral = args.regex === false;

  let re: RegExp;
  try {
    re = new RegExp(asLiteral ? escapeRegExp(query) : query);
  } catch {
    re = new RegExp(escapeRegExp(query)); // bad regex -> literal fallback
  }

  const rules = await loadIgnore(deps.cwd);
  const startInfo = await lstat(start);
  if (!startInfo.isFile() && !startInfo.isDirectory()) {
    throw new ToolError("search path must be a regular file or directory");
  }
  const startRel = relative(root, start).split(sep).join("/");
  const segments = startRel.split("/").filter(Boolean);
  // A scoped search bypasses the walk's ancestor pruning. Apply those same
  // rules to its root so a file/directory path cannot bypass .gitignore.
  for (let i = 1; i <= segments.length; i++) {
    const isDir = i < segments.length || startInfo.isDirectory();
    if (ignoredBy(rules, segments.slice(0, i).join("/"), isDir)) {
      throw new ToolError("search path is ignored; choose a non-ignored file or directory");
    }
  }
  const hits: string[] = [];
  let truncated = false;

  // `start` is canonical, so the walk is rooted at the canonical root too and
  // every yielded path stays relative to it.
  const files = startInfo.isFile() ? [startRel] : walkFiles(start, root, rules);
  search: for await (const rel of files) {
    const abs = join(root, rel);
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
 * glob - find files by name pattern (read-only, auto-runs). `**` crosses
 * directories; `*` and `?` stay within a segment. Honors .gitignore and caps at
 * MAX_GLOB_MATCHES (noting truncation).
 */
async function globTool(
  args: Record<string, unknown>,
  deps: ToolDeps,
): Promise<string> {
  const pattern = requireString(args, "pattern");
  const re = new RegExp(`^${globToRegExp(pattern)}$`);
  const root = await canonicalRoot(deps.cwd);
  const rules = await loadIgnore(deps.cwd);
  const matches: string[] = [];
  let truncated = false;

  for await (const rel of walkFiles(root, root, rules)) {
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
  const abs = await resolveInCwd(deps.cwd, path);

  let current = "";
  let before: Buffer | null = null;
  let exists = false;
  try {
    before = await readFile(abs);
    current = before.toString("utf8");
    exists = true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new ToolError(`cannot read existing file ${path}; refusing to overwrite it`);
    }
    current = ""; // genuinely new file
  }

  if (exists && current === content) {
    throw new ToolError(`file ${path} already has exactly this content; nothing changed. Make a different targeted correction.`);
  }

  // Guard against the classic weak-model mistake: overwriting an existing,
  // non-empty file with empty content. Weak models sometimes blank a file
  // with a pointless empty write after already making the real edit. Refuse
  // hard so --yes can't auto-approve a destructive no-op.
  if (current.trim().length > 0 && content.trim().length === 0) {
    throw new ToolError(
      `refusing to overwrite ${path} (${current.length} bytes) with empty content. ` +
        `Use edit_file for a targeted change instead.`,
    );
  }

  // Guard against blind-overwrite: rewriting an existing, non-empty file the
  // agent never read this task. Weak models sometimes rewrite a file from memory
  // and silently drop an existing function. Force a read first, and steer toward a
  // targeted edit_file over a whole-file overwrite.
  if (current.trim().length > 0 && deps.readPaths && !deps.readPaths.has(abs)) {
    throw new ToolError(
      `refusing to overwrite existing file ${path} without reading it first. ` +
        `read_file it, then use edit_file for a targeted change (write_file replaces ` +
        `the WHOLE file and will drop anything you don't re-include).`,
    );
  }

  const summary = unifiedDiff(path, current, content);
  const approved = await deps.confirm({
    tool: "write_file",
    summary,
    subject: path,
  });
  deps.signal?.throwIfAborted();
  if (!approved) return "user declined";

  await checkApprovedFile(deps, path, abs, before);
  await mkdir(dirname(abs), { recursive: true });
  try {
    // Exclusive creation also protects a new file appearing after the recheck.
    await writeFile(abs, content, { encoding: "utf8", flag: exists ? "w" : "wx" });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") throw staleApproval(path);
    throw error;
  }
  // After authoring the file, the agent knows its content - a later overwrite is
  // informed, not blind, so record it like a read.
  deps.readPaths?.add(abs);
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

  // Tier 3: uniform-indent - the whole block written at a consistent wrong
  // indent. We only accept a single uniform offset (reject non-uniform drift
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

/** Bounded, literal file context. Never guess a different edit or target file. */
function editContext(path: string, content: string, anchor = ""): string {
  const budget = 3000;
  const offset = anchor ? Math.max(0, content.indexOf(anchor)) : 0;
  const start = content.length <= budget ? 0 : Math.max(0, content.lastIndexOf("\n", Math.max(0, offset - 500)) + 1);
  const end = Math.min(content.length, start + budget);
  const partial = start > 0 || end < content.length;
  return `\nCURRENT FILE ${JSON.stringify(path)}${partial ? " (excerpt; use read_file for more)" : ""}:\n` +
    (start ? "[earlier content omitted]\n" : "") + content.slice(start, end) +
    (end < content.length ? "\n[later content omitted]" : "") + "\nEND CURRENT FILE";
}

/**
 * edit_file - replace `old_string` with `new_string` in an existing file. The
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
  const abs = await resolveInCwd(deps.cwd, path);

  if (oldString.length === 0) {
    throw new ToolError("old_string must not be empty (use write_file for new files)");
  }
  let current: string;
  let before: Buffer;
  try {
    before = await readFile(abs);
    current = before.toString("utf8");
  } catch {
    throw new ToolError(
      `file does not exist: ${path} (use write_file to create new files)`,
    );
  }

  if (oldString === newString) {
    throw new ToolError("old_string and new_string are identical: nothing to change. Inspect the actual file and make a different correction." + editContext(path, current, oldString));
  }

  const replaceAll = args.replace_all === true;
  const result = resolveEdit(current, oldString, newString, replaceAll, path);
  if ("error" in result) throw new ToolError(result.error +
    " Check the path and current contents; search_files can locate the definition in another file. Nothing was changed." +
    editContext(path, current, oldString));
  if (result.updated === current) {
    throw new ToolError("replacement matches the existing content after whitespace matching; nothing changed" + editContext(path, current, oldString));
  }

  const summary = unifiedDiff(path, current, result.updated);
  const approved = await deps.confirm({ tool: "edit_file", summary, subject: path });
  deps.signal?.throwIfAborted();
  if (!approved) return "user declined";

  await checkApprovedFile(deps, path, abs, before);
  await writeFile(abs, result.updated, "utf8");
  const parts = [`edited ${path}`];
  if (result.count > 1) parts.push(`(${result.count} occurrences)`);
  if (result.fuzzy) parts.push("(matched with whitespace flexibility)");
  return parts.join(" ") + editContext(path, result.updated, newString);
}

async function execCommand(
  command: string,
  cwd: string,
  onStarted?: () => void,
  signal?: AbortSignal,
): Promise<{ code: number; output: string }> {
  let output = "";
  let truncated = false;
  try {
    const code = await commandProcess(command, cwd, { signal, onStarted, onData: buf => {
      if (truncated) return;
      output += buf.toString("utf8");
      if (output.length >= MAX_CMD_OUTPUT) {
        output = output.slice(0, MAX_CMD_OUTPUT);
        truncated = true;
      }
    } });
    return { code, output: output + (truncated ? "\n…[output truncated]" : "") };
  } catch (error) {
    signal?.throwIfAborted();
    return { code: -1, output: `failed to start: ${(error as Error).message}` };
  }
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

  const { code, output } = await execCommand(command, deps.cwd, deps.onCommandStarted, deps.signal);
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
 * Run the configured verify command (build/tests) after an edit. Same child_process
 * spawn as run_command, but it is not gated (the user configured it) and the
 * captured output is tail-capped - build errors land at the end.
 */
export async function runVerify(command: string, cwd: string, signal?: AbortSignal): Promise<VerifyOutcome> {
  let buf = "";
  try {
    const code = await commandProcess(command, cwd, { signal, onData: b => {
      buf += b.toString("utf8");
      if (buf.length > VERIFY_BUFFER_BYTES) buf = buf.slice(buf.length - VERIFY_BUFFER_BYTES);
    } });
    return { code, output: tailCap(buf) };
  } catch (error) {
    signal?.throwIfAborted();
    return { code: -1, output: `failed to start: ${(error as Error).message}` };
  }
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
    deps.signal?.throwIfAborted();
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
    deps.signal?.throwIfAborted();
    const message = err instanceof Error ? err.message : String(err);
    return `error: ${message}`;
  }
}
