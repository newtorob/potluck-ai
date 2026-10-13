/**
 * Handoff snapshots: a short brief the next session reads after the context
 * is cleared. Shared with Claude Code and Codex through one store
 * ($SNAPSHOT_DIR, default ~/.snapshots) with one file per project root, so
 * the file format and path rule here must match ~/.snapshots/bin/snapshot.
 */

import { execFile } from "node:child_process";
import { copyFile, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

const exec = promisify(execFile);

export const DEFAULT_SNAPSHOT_DIR = join(homedir(), ".snapshots");

/** Store directory: $SNAPSHOT_DIR, else ~/.snapshots. */
export function snapshotDir(env: NodeJS.ProcessEnv = process.env): string {
  const dir = env.SNAPSHOT_DIR?.trim();
  return dir ? dir : DEFAULT_SNAPSHOT_DIR;
}

/** "/Users/sam/code/app" -> "Users-sam-code-app". */
export function projectKey(root: string): string {
  return root.replace(/^\/+/, "").replace(/\//g, "-");
}

async function git(cwd: string, args: string[]): Promise<string | null> {
  try {
    const { stdout } = await exec("git", ["-C", cwd, ...args]);
    return stdout.replace(/\n$/, "");
  } catch {
    return null;
  }
}

/** Git toplevel when inside a repo, otherwise the directory itself. */
export async function projectRoot(cwd: string): Promise<string> {
  const top = await git(cwd, ["rev-parse", "--show-toplevel"]);
  return top && top.trim() ? top.trim() : cwd;
}

export function snapshotPath(root: string, opts: { dir?: string } = {}): string {
  return join(opts.dir ?? snapshotDir(), `${projectKey(root)}.md`);
}

export interface SnapshotFile {
  text: string;
  ageMs: number;
}

export async function readSnapshot(
  file: string,
  now: number = Date.now(),
): Promise<SnapshotFile | null> {
  try {
    const [text, info] = await Promise.all([readFile(file, "utf8"), stat(file)]);
    return { text, ageMs: Math.max(0, now - info.mtimeMs) };
  } catch {
    return null;
  }
}

export interface SnapshotParts {
  title: string;
  header: string;
  body: string;
}

/** Write the snapshot, keeping the previous one as <key>.prev.md. */
export async function writeSnapshot(file: string, parts: SnapshotParts): Promise<void> {
  await mkdir(dirname(file), { recursive: true });
  try {
    await copyFile(file, file.replace(/\.md$/, ".prev.md"));
  } catch {
    // no previous snapshot
  }
  const body = parts.body.replace(/\s+$/, "");
  await writeFile(file, `# Snapshot: ${parts.title}\n${parts.header}\n\n${body}\n`, "utf8");
}

export function formatAge(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 120) return `${s} seconds`;
  if (s < 7200) return `${Math.floor(s / 60)} minutes`;
  if (s < 172800) return `${Math.floor(s / 3600)} hours`;
  return `${Math.floor(s / 86400)} days`;
}

function formatLocal(d: Date): string {
  const pad = (n: number): string => String(n).padStart(2, "0");
  const tz =
    new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
      .formatToParts(d)
      .find((p) => p.type === "timeZoneName")?.value ?? "";
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())} ${tz}`.trim();
}

/** One header line: when, which harness, project root, and git state. */
export async function snapshotHeader(
  harness: string,
  root: string,
  now: Date = new Date(),
): Promise<string> {
  let gitPart = "";
  if ((await git(root, ["rev-parse", "--is-inside-work-tree"])) === "true") {
    const branch = (await git(root, ["rev-parse", "--abbrev-ref", "HEAD"])) ?? "?";
    const commit = (await git(root, ["rev-parse", "--short", "HEAD"])) ?? "?";
    const status = (await git(root, ["status", "--porcelain"])) ?? "";
    const dirty = status.split("\n").filter((l) => l.length > 0).length;
    gitPart = ` | branch: ${branch} @ ${commit} | dirty files: ${dirty}`;
  }
  return `- Written: ${formatLocal(now)} by ${harness} | project: ${root}${gitPart}`;
}

/** System-prompt section that carries the brief into a fresh conversation. */
export function briefSection(text: string, file: string, ageMs: number): string {
  return [
    "Handoff brief:",
    `A previous session in this project saved the snapshot below before its context was cleared (file: ${file}, written ${formatAge(ageMs)} ago). Read it before doing anything else. On your first reply, say in one or two lines what you understand the next step to be, then wait for the user to confirm. Do not start work unprompted. If the user asks for a fresh start, ignore the brief.`,
    "",
    text.trimEnd(),
  ].join("\n");
}

/** The prompt /snapshot sends; the model answers in prose and the CLI saves it. */
export function snapshotRequest(): string {
  return [
    "Write a handoff brief for the next session, which starts with an empty context.",
    "Reply in prose only: no action block, and no code fence around the answer.",
    "Under 80 lines. Use exactly these six sections in this order:",
    "",
    "## Goal",
    "What we are building and why. One to three sentences.",
    "## Where things stand",
    "What is done. Mark each item verified (build or tests ran, output seen) or unverified. Name uncommitted files.",
    "## Next steps",
    "Numbered, in order. The first item is what to do immediately.",
    "## Decisions and constraints",
    "Choices made and why. Rules the user set.",
    "## Open questions",
    "What the user still has to decide. Blockers.",
    "## Files and commands that matter",
    "Paths, and the exact commands to build, test, or run.",
    "",
    "Do not claim anything works that was not run. Do not commit, stash, or write files; the CLI saves this brief itself.",
  ].join("\n");
}

/** Pull the brief out of the model's final answer; "" when there is none. */
export function extractSnapshotBody(answer: string): string {
  let text = answer.trim();
  const fence = /^```(\w*)[ \t]*\n([\s\S]*?)\n?```\s*$/.exec(text);
  if (fence) {
    if (fence[1] === "action") return "";
    text = fence[2].trim();
  }
  if (/^```action/m.test(text)) return "";
  return text;
}
