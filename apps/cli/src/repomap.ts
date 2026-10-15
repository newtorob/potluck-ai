/**
 * A compact repository map injected once at session start.
 *
 * Two problems it solves for weak local models: they waste turns on
 * glob/list_dir just to learn what exists, and they hallucinate paths (for
 * example, inventing a `src/` directory and writing to the wrong file). Giving
 * the real file list and bounded declaration hints up front helps locate the
 * owning file. Hints are heuristic, not a parser; inspect source before editing.
 */

import { loadIgnore, walkFiles } from "./walk.js";
import { open } from "node:fs/promises";
import { constants } from "node:fs";
import { extname, join } from "node:path";

const MAX_MAP_FILES = 200;
const MAX_SYMBOL_FILES = 50;
const MAX_SYMBOL_BYTES = 32_000;
const MAX_SYMBOL_CHARS = 3000;

/** Only names/line numbers, never bodies or values. Ignore unsupported files. */
async function declarations(cwd: string, path: string): Promise<string> {
  const ext = extname(path).toLowerCase();
  const python = ext === ".py";
  if (!python && ![".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"].includes(ext)) return "";
  let file;
  try {
    file = await open(join(cwd, path), constants.O_RDONLY | constants.O_NOFOLLOW);
    if (!(await file.stat()).isFile()) return "";
    const buffer = Buffer.alloc(MAX_SYMBOL_BYTES);
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
    if (buffer.subarray(0, bytesRead).includes(0)) return "";
    const content = buffer.subarray(0, bytesRead).toString("utf8");
    // Discard a potentially partial final line in a capped read.
    const lines = (bytesRead === MAX_SYMBOL_BYTES ? content.slice(0, Math.max(0, content.lastIndexOf("\n"))) : content).split("\n");
    const hints: string[] = [];
    for (let i = 0; i < lines.length && hints.length < 6; i++) {
      const match = python
        ? /^(?:async\s+)?(?:def|class)\s+([A-Za-z_][\w]{0,79})\b/.exec(lines[i])
        : /^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s*\*?|class|interface|type|enum|const|let)\s+([A-Za-z_$][\w$]{0,79})\b/.exec(lines[i]);
      if (match) hints.push(`${match[1]}:${i + 1}`);
    }
    return hints.length ? `  declarations (name:line): ${hints.join(", ")}` : "";
  } catch {
    return ""; // Deleted, unreadable, or changed into a symlink since the walk.
  } finally {
    await file?.close();
  }
}

/** Build the repo-map text (empty string if the directory has no files). */
export async function buildRepoMap(cwd: string, limit = MAX_MAP_FILES, maxBytes?: number): Promise<string> {
  const rules = await loadIgnore(cwd);
  const files: string[] = [];
  let truncated = false;
  for await (const rel of walkFiles(cwd, cwd, rules)) {
    files.push(rel);
    if (files.length >= limit) {
      truncated = true;
      break;
    }
  }
  if (files.length === 0) return "";
  files.sort((a, b) => a.localeCompare(b));

  const lines = [
    "Repository files (relative paths; partial list). Use exact paths and search for others.",
    "Declaration hints are a bounded heuristic from the initial files; read_file to confirm.",
  ];
  let symbolFiles = 0;
  let symbolChars = 0;
  for (const path of files) {
    lines.push(path);
    if (symbolFiles >= MAX_SYMBOL_FILES || symbolChars >= MAX_SYMBOL_CHARS) continue;
    if (!/\.(py|[cm]?js|jsx|tsx?)$/i.test(path)) continue;
    symbolFiles++;
    const hints = await declarations(cwd, path);
    if (hints && symbolChars + hints.length <= MAX_SYMBOL_CHARS) {
      lines.push(hints);
      symbolChars += hints.length;
    }
  }
  if (truncated) lines.push(`(+ more files; showing the first ${limit})`);
  const text = lines.join("\n");
  if (maxBytes === undefined || Buffer.byteLength(text) <= maxBytes) return text;
  const marker = "\n(+ more files; use glob or search_files)";
  if (maxBytes < Buffer.byteLength(marker)) return "";
  const bounded: string[] = [];
  for (const line of lines) {
    if (Buffer.byteLength([...bounded, line].join("\n") + marker) > maxBytes) break;
    bounded.push(line);
  }
  return bounded.join("\n") + marker;
}
