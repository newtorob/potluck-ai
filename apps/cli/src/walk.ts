/**
 * Filesystem walking + a pragmatic .gitignore matcher, shared by search_files
 * and glob. No external deps: each ignore line is compiled to a RegExp, and a
 * small glob→regex translator backs both ignore matching and the glob tool.
 *
 * The matcher is a useful subset of real gitignore semantics: comments, blank
 * lines, negation (!), anchoring (leading or interior /), directory-only (/),
 * and *, **, ? wildcards. `.git/` and `node_modules/` are always ignored.
 */

import { readdir, readFile } from "node:fs/promises";
import { join, relative, sep } from "node:path";

export interface IgnoreRule {
  re: RegExp;
  negate: boolean;
  dirOnly: boolean;
}

function escapeRegExpChar(ch: string): string {
  return /[.*+?^${}()|[\]\\]/.test(ch) ? `\\${ch}` : ch;
}

/** Escape a literal string for safe use inside a RegExp. */
export function escapeRegExp(literal: string): string {
  return literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Translate a glob (`*`, `**`, `?`) into a regex fragment. `*` matches within a
 * path segment; `**` (optionally `**​/`) matches across segments.
 */
export function globToRegExp(pattern: string): string {
  let out = "";
  for (let i = 0; i < pattern.length; i++) {
    const ch = pattern[i];
    if (ch === "*") {
      if (pattern[i + 1] === "*") {
        i++; // consume the second '*'
        if (pattern[i + 1] === "/") {
          out += "(?:.*/)?"; // `**/` — zero or more leading directories
          i++;
        } else {
          out += ".*"; // `**` — anything, including separators
        }
      } else {
        out += "[^/]*"; // `*` — anything except a separator
      }
    } else if (ch === "?") {
      out += "[^/]";
    } else {
      out += escapeRegExpChar(ch);
    }
  }
  return out;
}

/** Compile raw .gitignore lines into ordered rules. */
export function compileIgnore(lines: string[]): IgnoreRule[] {
  const rules: IgnoreRule[] = [];
  for (const raw of lines) {
    const line = raw.replace(/\r$/, "").trim();
    if (line === "" || line.startsWith("#")) continue;

    let p = line;
    let negate = false;
    if (p.startsWith("!")) {
      negate = true;
      p = p.slice(1);
    }

    let dirOnly = false;
    if (p.endsWith("/")) {
      dirOnly = true;
      p = p.slice(0, -1);
    }

    let anchored = false;
    if (p.startsWith("/")) {
      anchored = true;
      p = p.slice(1);
    } else if (p.includes("/")) {
      anchored = true; // an interior slash anchors to the gitignore root
    }

    const body = globToRegExp(p);
    const prefix = anchored ? "^" : "(?:^|.*/)";
    rules.push({ re: new RegExp(`${prefix}${body}$`), negate, dirOnly });
  }
  return rules;
}

/** Test a path (relative, "/"-separated) against compiled ignore rules. */
export function ignoredBy(
  rules: IgnoreRule[],
  relPath: string,
  isDir: boolean,
): boolean {
  const path = relPath.split(sep).join("/");
  let ignored = false;
  for (const r of rules) {
    // Directory-only rules never match a plain file directly; descendants of an
    // ignored directory are pruned during the walk instead.
    if (r.dirOnly && !isDir) continue;
    if (r.re.test(path)) ignored = !r.negate;
  }
  return ignored;
}

/** Load the ignore rules for `root` (its .gitignore plus the always-skip set). */
export async function loadIgnore(root: string): Promise<IgnoreRule[]> {
  const lines = [".git/", "node_modules/"];
  try {
    const text = await readFile(join(root, ".gitignore"), "utf8");
    lines.push(...text.split("\n"));
  } catch {
    // No .gitignore — defaults still apply.
  }
  return compileIgnore(lines);
}

/**
 * Walk files under `startDir`, yielding each file path relative to `ignoreRoot`
 * (with "/" separators), pruning ignored directories. Entries are visited in a
 * stable, name-sorted order.
 *
 * Symlinked entries are skipped outright, which is what keeps the walk inside
 * the workspace: a symlinked directory can point anywhere, and following it
 * would hand search_files and glob the contents of files outside the workspace
 * (audit P1). The alternative, realpath-ing each symlink and keeping the ones
 * that land inside, costs a syscall per entry and reopens the same check-then-use
 * race the walk would otherwise not have, for the small benefit of listing
 * in-workspace aliases; symlinks are skipped instead. Note this was already the
 * de facto behavior (readdir's Dirent reports a symlink as neither a file nor a
 * directory), so the guard below makes an accident explicit rather than changing
 * what the walk yields. Callers still have to confine `startDir` themselves:
 * readdir DOES follow a symlink handed to it as the walk root.
 */
export async function* walkFiles(
  startDir: string,
  ignoreRoot: string,
  rules: IgnoreRule[],
): AsyncGenerator<string> {
  async function* recurse(dir: string): AsyncGenerator<string> {
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      return; // unreadable directory — skip it rather than crash
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const e of entries) {
      if (e.isSymbolicLink()) continue; // never cross a symlink (see above)
      const abs = join(dir, e.name);
      const rel = relative(ignoreRoot, abs).split(sep).join("/");
      if (e.isDirectory()) {
        if (ignoredBy(rules, rel, true)) continue;
        yield* recurse(abs);
      } else if (e.isFile()) {
        if (ignoredBy(rules, rel, false)) continue;
        yield rel;
      }
    }
  }
  yield* recurse(startDir);
}
