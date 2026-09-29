import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
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

  it("maps definitions to their owning file without including bodies or values", async () => {
    await writeFile(join(cwd, "caller.py"), "from worker import transform\n\ndef run():\n    return transform()\n");
    await writeFile(join(cwd, "worker.py"), "async def transform():\n    return 'private-value'\n");
    const map = await buildRepoMap(cwd);
    expect(map).toContain("caller.py\n  declarations (name:line): run:3");
    expect(map).toContain("worker.py\n  declarations (name:line): transform:1");
    expect(map).not.toContain("private-value");
  });

  it("supports exported JS/TS declarations but skips binary, ignored and symlinked files", async () => {
    await writeFile(join(cwd, "module.ts"), "export async function run() {}\nexport const enabled = false;\n");
    await writeFile(join(cwd, "binary.py"), "def fake():\n\0binary");
    await writeFile(join(cwd, ".gitignore"), "hidden.py\n");
    await writeFile(join(cwd, "hidden.py"), "def secret(): pass\n");
    await symlink(join(cwd, "hidden.py"), join(cwd, "alias.py"));
    const map = await buildRepoMap(cwd);
    expect(map).toContain("run:1, enabled:2");
    expect(map).not.toContain("false");
    expect(map).not.toContain("fake:");
    expect(map).not.toContain("secret:");
    expect(map).not.toContain("alias.py");
  });

  it("caps large files and total declaration text while retaining the file list", async () => {
    await writeFile(join(cwd, "large.py"), "# padding\n".repeat(4000) + "def beyond_cap(): pass\n");
    for (let i = 0; i < 60; i++) {
      await writeFile(join(cwd, `file${i}.py`), Array.from({ length: 9 }, (_, j) => `def name_${'x'.repeat(60)}_${j}(): pass`).join("\n"));
    }
    const map = await buildRepoMap(cwd);
    expect(map).toContain("large.py");
    expect(map).toContain("file59.py");
    expect(map).not.toContain("beyond_cap:");
    expect(map).not.toContain("_8:");
    expect(map.split("\n").filter(l => l.startsWith("  declarations")).join("").length).toBeLessThanOrEqual(3000);
  });
});
