import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, stat, utimes, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import { afterEach, describe, expect, it } from "vitest";

import { systemPrompt } from "../src/agent.js";
import {
  briefSection,
  extractSnapshotBody,
  formatAge,
  projectKey,
  projectRoot,
  readSnapshot,
  snapshotHeader,
  snapshotPath,
  snapshotRequest,
  writeSnapshot,
} from "../src/snapshot.js";

const exec = promisify(execFile);
const cleanups: string[] = [];

async function tmp(prefix: string): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), prefix));
  cleanups.push(dir);
  return dir;
}

async function gitRepo(): Promise<string> {
  const dir = await tmp("snap-repo-");
  await exec("git", ["init", "-q"], { cwd: dir });
  await writeFile(join(dir, "a.txt"), "a\n");
  await exec("git", ["add", "-A"], { cwd: dir });
  await exec(
    "git",
    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    { cwd: dir },
  );
  return dir;
}

afterEach(async () => {
  for (const dir of cleanups.splice(0)) await rm(dir, { recursive: true, force: true });
});

describe("projectKey", () => {
  it("drops the leading slash and joins path segments with dashes", () => {
    expect(projectKey("/Users/sam/Documents/potluckai")).toBe(
      "Users-rob-Documents-potluckai",
    );
  });
});

describe("snapshotPath", () => {
  it("puts <key>.md under the given store directory", () => {
    expect(snapshotPath("/Users/sam/x", { dir: "/store" })).toBe("/store/Users-rob-x.md");
  });
});

describe("projectRoot", () => {
  it("resolves a subdirectory of a git repo to the repo root", async () => {
    const repo = await gitRepo();
    const sub = join(repo, "src", "deep");
    await mkdir(sub, { recursive: true });
    const root = await projectRoot(sub);
    expect((await stat(root)).ino).toBe((await stat(repo)).ino);
  });

  it("returns the directory itself when it is not inside a git repo", async () => {
    const plain = await tmp("snap-plain-");
    expect(await projectRoot(plain)).toBe(plain);
  });
});

describe("writeSnapshot / readSnapshot", () => {
  it("returns null when no snapshot exists", async () => {
    const store = await tmp("snap-store-");
    expect(await readSnapshot(join(store, "nope.md"))).toBeNull();
  });

  it("writes title, header, and body, creating the store directory", async () => {
    const store = await tmp("snap-store-");
    const file = join(store, "nested", "proj.md");
    await writeSnapshot(file, { title: "proj", header: "- Written: now by potluck", body: "## Goal\nShip it.\n" });
    expect(await readFile(file, "utf8")).toBe(
      "# Snapshot: proj\n- Written: now by potluck\n\n## Goal\nShip it.\n",
    );
  });

  it("keeps the previous snapshot as <key>.prev.md when overwriting", async () => {
    const store = await tmp("snap-store-");
    const file = join(store, "proj.md");
    await writeSnapshot(file, { title: "proj", header: "h1", body: "first" });
    await writeSnapshot(file, { title: "proj", header: "h2", body: "second" });
    expect(await readFile(join(store, "proj.prev.md"), "utf8")).toContain("first");
    expect(await readFile(file, "utf8")).toContain("second");
  });

  it("reads the text back with an age derived from the file mtime", async () => {
    const store = await tmp("snap-store-");
    const file = join(store, "proj.md");
    await writeSnapshot(file, { title: "proj", header: "h", body: "b" });
    const old = new Date(Date.now() - 3 * 3600_000);
    await utimes(file, old, old);
    const snap = await readSnapshot(file, Date.now());
    expect(snap?.text).toContain("# Snapshot: proj");
    expect(snap?.ageMs).toBeGreaterThanOrEqual(3 * 3600_000 - 2000);
    expect(snap?.ageMs).toBeLessThan(4 * 3600_000);
  });
});

describe("formatAge", () => {
  it("picks the unit the way the shell script does", () => {
    expect(formatAge(45_000)).toBe("45 seconds");
    expect(formatAge(5 * 60_000)).toBe("5 minutes");
    expect(formatAge(3 * 3600_000)).toBe("3 hours");
    expect(formatAge(2 * 86400_000)).toBe("2 days");
  });
});

describe("snapshotHeader", () => {
  it("includes harness, project root, and git state inside a repo", async () => {
    const repo = await gitRepo();
    await writeFile(join(repo, "dirty.txt"), "x\n");
    const header = await snapshotHeader("potluck", repo, new Date(2026, 8, 7, 9, 5));
    expect(header).toMatch(/^- Written: 2026-09-07 09:05 \S+ by potluck \| project: /);
    expect(header).toContain(`| branch: `);
    expect(header).toMatch(/@ [0-9a-f]{7}/);
    expect(header).toContain("| dirty files: 1");
  });

  it("omits the git part outside a repo", async () => {
    const plain = await tmp("snap-plain-");
    const header = await snapshotHeader("potluck", plain, new Date());
    expect(header).toContain(`| project: ${plain}`);
    expect(header).not.toContain("branch:");
  });
});

describe("briefSection", () => {
  it("wraps the snapshot with where it came from and how to act on it", () => {
    const section = briefSection("# Snapshot: proj\nbody", "/store/proj.md", 300_000);
    expect(section).toContain("Handoff brief");
    expect(section).toContain("/store/proj.md");
    expect(section).toContain("written 5 minutes ago");
    expect(section).toContain("# Snapshot: proj\nbody");
    expect(section).toMatch(/wait for the user/i);
  });
});

describe("systemPrompt with a brief", () => {
  it("appends the handoff brief so it survives history trimming at index 0", () => {
    const prompt = systemPrompt("/proj", null, "HANDOFF-MARKER");
    expect(prompt).toContain("HANDOFF-MARKER");
    expect(prompt.indexOf("Tools:")).toBeLessThan(prompt.indexOf("HANDOFF-MARKER"));
  });

  it("is unchanged when no brief is given", () => {
    expect(systemPrompt("/proj", null)).toBe(systemPrompt("/proj", null, null));
  });
});

describe("snapshotRequest", () => {
  it("asks for the fixed sections in prose, not an action block", () => {
    const req = snapshotRequest();
    for (const h of ["## Goal", "## Where things stand", "## Next steps", "## Decisions and constraints", "## Open questions", "## Files and commands that matter"]) {
      expect(req).toContain(h);
    }
    expect(req).toMatch(/no action block/i);
  });
});

describe("extractSnapshotBody", () => {
  it("strips a surrounding markdown fence and trims", () => {
    expect(extractSnapshotBody("```markdown\n## Goal\nx\n```\n")).toBe("## Goal\nx");
  });

  it("returns prose untouched", () => {
    expect(extractSnapshotBody("## Goal\nx\n")).toBe("## Goal\nx");
  });

  it("returns an empty string for an action block or nothing usable", () => {
    expect(extractSnapshotBody('```action\n{"tool":"read_file"}\n```')).toBe("");
    expect(extractSnapshotBody("   ")).toBe("");
  });
});
