import { mkdtemp, readFile, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTool, type Confirm } from "../src/tools.js";

let cwd = "";
beforeEach(async () => { cwd = await mkdtemp(join(tmpdir(), "potluck-concurrent-files-")); });
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });

const proposed = (tool: "write_file" | "edit_file", path = "code.txt") => tool === "write_file"
  ? { path, content: "agent change\n" }
  : { path, old_string: "original", new_string: "agent change" };

describe.each(["write_file", "edit_file"] as const)("%s approval freshness", (tool) => {
  it("preserves an editor's changes made while the approval prompt is open", async () => {
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    const confirm = vi.fn<Confirm>(async () => {
      await writeFile(path, "original\nnew editor work\n");
      return true;
    });
    const result = await runTool(tool, proposed(tool), { cwd, confirm });
    expect(confirm).toHaveBeenCalledOnce();
    expect(result).toMatch(/^error:.*changed.*approval/);
    expect(await readFile(path, "utf8")).toBe("original\nnew editor work\n");
  });

  it("does not recreate a file deleted while approval is pending", async () => {
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    const result = await runTool(tool, proposed(tool), {
      cwd, confirm: async () => { await unlink(path); return true; },
    });
    expect(result).toMatch(/^error:.*changed.*approval/);
    await expect(readFile(path)).rejects.toMatchObject({ code: "ENOENT" });
  });

  it.skipIf(process.platform === "win32")("rejects a symlink retargeted while approval is pending", async () => {
    await writeFile(join(cwd, "first.txt"), "original\n");
    await writeFile(join(cwd, "second.txt"), "other work\n");
    await symlink("first.txt", join(cwd, "code.txt"));
    const result = await runTool(tool, proposed(tool), {
      cwd,
      confirm: async () => {
        await unlink(join(cwd, "code.txt"));
        await symlink("second.txt", join(cwd, "code.txt"));
        return true;
      },
    });
    expect(result).toMatch(/^error:.*changed.*approval/);
    expect(await readFile(join(cwd, "first.txt"), "utf8")).toBe("original\n");
    expect(await readFile(join(cwd, "second.txt"), "utf8")).toBe("other work\n");
  });

  it("accepts a fresh approval after detecting a conflict", async () => {
    const path = join(cwd, "code.txt");
    await writeFile(path, "original\n");
    await runTool(tool, proposed(tool), {
      cwd, confirm: async () => { await writeFile(path, "original\neditor work\n"); return true; },
    });
    const confirm = vi.fn<Confirm>(async () => true);
    const args = tool === "write_file"
      ? { path: "code.txt", content: "agent change\neditor work\n" }
      : proposed(tool);
    const result = await runTool(tool, args, { cwd, confirm });
    expect(result).toMatch(/^(wrote|edited)/);
    expect(await readFile(path, "utf8")).toBe("agent change\neditor work\n");
    expect(confirm.mock.calls[0]?.[0].summary).toContain("editor work");
  });
});

it.each(["", "created by another process\n"])("does not overwrite a new file that appeared during approval (%j)", async (content) => {
  const path = join(cwd, "code.txt");
  const result = await runTool("write_file", proposed("write_file"), {
    cwd, confirm: async () => { await writeFile(path, content); return true; },
  });
  expect(result).toMatch(/^error:.*changed.*approval/);
  expect(await readFile(path, "utf8")).toBe(content);
});
