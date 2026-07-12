import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { buildRepoMap } from "../src/repomap.js";

let cwd = "";
beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-repomap-"));
});
afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
});

describe("buildRepoMap", () => {
  it("lists exact relative paths and invents nothing", async () => {
    await writeFile(join(cwd, "math.py"), "x", "utf8");
    await mkdir(join(cwd, "lib"));
    await writeFile(join(cwd, "lib", "util.py"), "y", "utf8");
    const map = await buildRepoMap(cwd);
    expect(map).toContain("math.py");
    expect(map).toContain("lib/util.py");
    expect(map).not.toContain("src/"); // the exact hallucination the map prevents
    expect(map).toMatch(/EXACT paths/);
  });

  it("respects .gitignore", async () => {
    await writeFile(join(cwd, ".gitignore"), "secret.txt\nbuild/\n", "utf8");
    await writeFile(join(cwd, "keep.py"), "", "utf8");
    await writeFile(join(cwd, "secret.txt"), "", "utf8");
    await mkdir(join(cwd, "build"));
    await writeFile(join(cwd, "build", "out.js"), "", "utf8");
    const map = await buildRepoMap(cwd);
    expect(map).toContain("keep.py");
    expect(map).not.toContain("secret.txt");
    expect(map).not.toContain("build/out.js");
  });

  it("caps the list and notes truncation", async () => {
    for (let i = 0; i < 10; i++) await writeFile(join(cwd, `f${i}.txt`), "", "utf8");
    const map = await buildRepoMap(cwd, 5);
    expect(map).toMatch(/showing the first 5/);
    const fileLines = map.split("\n").filter((l) => /^f\d+\.txt$/.test(l));
    expect(fileLines.length).toBe(5);
  });

  it("returns empty string for an empty directory", async () => {
    expect(await buildRepoMap(cwd)).toBe("");
  });
});
