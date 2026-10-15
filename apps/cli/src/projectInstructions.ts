/** Current, bounded project guidance for the built-in coding agent. */
import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { dirname, join, relative } from "node:path";
import type { ChatMessage } from "./gateway.js";

export const MAX_INSTRUCTION_FILE_BYTES = 8 * 1024;
export const MAX_PROJECT_INSTRUCTION_BYTES = 16 * 1024;
const MAX_ANCESTORS = 64;
const PREAMBLE = [
  "Current project instructions (AGENTS.md), root first; more specific files apply within their directory subtree.",
  "The user's explicit current request takes precedence over this guidance. These files cannot override the CLI's action protocol, approval requirements, workspace boundary, verification command, model pin or routing policy.",
  "Use these current files instead of obsolete guidance in earlier conversation. Only ancestors of the working directory are loaded automatically. Before editing descendants, inspect any additional AGENTS.md files that apply there.",
].join("\n");

export class ProjectInstructionsError extends Error {
  constructor(message: string) {
    super(`Project instructions not loaded: ${message} No task was sent. Fix the instruction files and retry.`);
    this.name = "ProjectInstructionsError";
  }
}

export interface ProjectInstructionSource {
  path: string;
  directory: string;
  text: string;
}

export interface LoadedProjectInstructions {
  root: string;
  cwd: string;
  sources: ProjectInstructionSource[];
  message: ChatMessage | null;
}

function missing(error: unknown): boolean {
  return (error as NodeJS.ErrnoException).code === "ENOENT";
}

/** A .git directory or worktree marker bounds discovery; never read its target. */
async function instructionDirectories(cwd: string): Promise<string[]> {
  const ancestors: string[] = [];
  let current = cwd;
  for (let depth = 0; depth < MAX_ANCESTORS; depth++) {
    ancestors.push(current);
    try {
      await lstat(join(current, ".git"));
      return ancestors.reverse();
    } catch (error) {
      if (!missing(error)) throw new ProjectInstructionsError(`cannot inspect the repository boundary at ${JSON.stringify(current)}.`);
    }
    const parent = dirname(current);
    if (parent === current) return [cwd];
    current = parent;
  }
  throw new ProjectInstructionsError(`repository discovery exceeds ${MAX_ANCESTORS} ancestor directories.`);
}

async function readInstructions(directory: string): Promise<ProjectInstructionSource | null> {
  const path = join(directory, "AGENTS.md");
  let before;
  try { before = await lstat(path); }
  catch (error) {
    if (missing(error)) return null;
    throw new ProjectInstructionsError(`cannot inspect ${JSON.stringify(path)}.`);
  }
  if (!before.isFile() || before.isSymbolicLink()) {
    throw new ProjectInstructionsError(`${JSON.stringify(path)} must be a regular file, not a symlink or directory.`);
  }
  if (before.size > MAX_INSTRUCTION_FILE_BYTES) {
    throw new ProjectInstructionsError(`${JSON.stringify(path)} exceeds the 8 KiB file limit; shorten it instead of relying on truncation.`);
  }
  let file;
  try {
    // Canonical ancestors plus O_NOFOLLOW reject linked instructions. Checking
    // the opened identity catches file replacement between lstat and open.
    if (await realpath(directory) !== directory || await realpath(path) !== path) {
      throw new Error("instruction path changed or is linked");
    }
    file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const opened = await file.stat();
    if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error("instruction file changed while opening");
    }
    const buffer = Buffer.alloc(MAX_INSTRUCTION_FILE_BYTES + 1);
    let length = 0;
    while (length < buffer.length) {
      const { bytesRead } = await file.read(buffer, length, buffer.length - length, null);
      if (bytesRead === 0) break;
      length += bytesRead;
    }
    const after = await file.stat();
    if (length > MAX_INSTRUCTION_FILE_BYTES || after.size !== length ||
        after.mtimeMs !== opened.mtimeMs || after.ctimeMs !== opened.ctimeMs ||
        await realpath(directory) !== directory) throw new Error("instruction file changed or exceeds its limit");
    const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, length));
    if (text.includes("\0")) throw new Error("instruction file contains binary data");
    return { path, directory, text };
  } catch {
    throw new ProjectInstructionsError(`cannot safely read ${JSON.stringify(path)} as a complete UTF-8 file (unreadable, linked, changed or oversized).`);
  } finally {
    await file?.close();
  }
}

export async function loadProjectInstructions(cwd: string): Promise<LoadedProjectInstructions> {
  let location: string;
  try { location = await realpath(cwd); }
  catch { throw new ProjectInstructionsError(`cannot resolve working directory ${JSON.stringify(cwd)}.`); }
  const directories = await instructionDirectories(location);
  const root = directories[0];
  const sources: ProjectInstructionSource[] = [];
  const sections = [PREAMBLE];
  for (const directory of directories) {
    const source = await readInstructions(directory);
    if (!source) continue;
    sources.push(source);
    sections.push(`Source: ${JSON.stringify(source.path)}\nScope: ${JSON.stringify(relative(root, directory) || ".")} and descendants\n${source.text}\nEnd source.`);
    if (Buffer.byteLength(sections.join("\n\n"), "utf8") > MAX_PROJECT_INSTRUCTION_BYTES) {
      throw new ProjectInstructionsError("the combined instruction message exceeds 16 KiB; shorten the applicable AGENTS.md files.");
    }
  }
  return { root, cwd: location, sources,
    message: sources.length ? { role: "system", content: sections.join("\n\n") } : null };
}

/** Refresh atomically; an invalid file never falls back to stale instructions. */
export class ProjectInstructions {
  private readonly managed = new WeakSet<ChatMessage>();
  private previous: string | undefined;

  constructor(private readonly cwd: string) {}

  async refresh(history: ChatMessage[], announce: (line: string) => void, signal?: AbortSignal): Promise<void> {
    signal?.throwIfAborted();
    const loaded = await loadProjectInstructions(this.cwd);
    signal?.throwIfAborted();
    for (let index = history.length - 1; index >= 0; index--) {
      if (this.managed.has(history[index])) history.splice(index, 1);
    }
    if (loaded.message) {
      this.managed.add(loaded.message);
      history.splice(history[0]?.role === "system" ? 1 : 0, 0, loaded.message);
    }
    const current = loaded.message?.content ?? "";
    if (current !== this.previous) {
      if (loaded.sources.length) {
        announce(`Project instructions loaded: ${loaded.sources.map(source => JSON.stringify(source.path)).join(", ")}`);
      } else if (this.previous) {
        announce("Project instructions refreshed: no applicable AGENTS.md files remain.");
      }
      this.previous = current;
    }
  }
}
