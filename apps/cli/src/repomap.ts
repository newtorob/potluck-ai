/**
 * A compact repository map injected once at session start.
 *
 * Two problems it solves for weak local models: they waste turns on
 * glob/list_dir just to learn what exists, and they hallucinate paths (seen
 * live: a model invented a `src/` directory and wrote to the wrong file). Giving
 * the real file list up front prevents both. Files-only for now (a symbol map is
 * a future enhancement); gitignore-respecting; capped so it can't blow the
 * context on a huge tree.
 */

import { loadIgnore, walkFiles } from "./walk.js";

const MAX_MAP_FILES = 200;

/** Build the repo-map text (empty string if the directory has no files). */
export async function buildRepoMap(cwd: string, limit = MAX_MAP_FILES): Promise<string> {
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
    "Repository files (relative to the working directory). Use these EXACT paths;",
    "do not invent files or directories that are not listed here:",
    ...files,
  ];
  if (truncated) lines.push(`(+ more files; showing the first ${limit})`);
  return lines.join("\n");
}
