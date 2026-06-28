import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTool, type Confirm } from "../src/tools.js";

let cwd = "";
const allow: Confirm = async () => true;
const deny: Confirm = async () => false;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-tools-"));
});

afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
});

describe("read_file", () => {
  it("returns file contents", async () => {
    await writeFile(join(cwd, "hello.txt"), "hi there", "utf8");
    const out = await runTool("read_file", { path: "hello.txt" }, { cwd, confirm: allow });
    expect(out).toBe("hi there");
  });

  it("returns an error string for a missing file (no throw)", async () => {
    const out = await runTool("read_file", { path: "nope.txt" }, { cwd, confirm: allow });
    expect(out).toMatch(/^error:/);
  });

  it("refuses paths that escape the working directory", async () => {
    const out = await runTool(
      "read_file",
      { path: "../escape.txt" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/escapes the working directory/);
  });
});

describe("list_dir", () => {
  it("lists entries with a trailing slash on directories", async () => {
    await writeFile(join(cwd, "a.txt"), "", "utf8");
    await mkdir(join(cwd, "sub"));
    const out = await runTool("list_dir", { path: "." }, { cwd, confirm: allow });
    expect(out.split("\n")).toContain("a.txt");
    expect(out.split("\n")).toContain("sub/");
  });
});

describe("write_file", () => {
  it("writes when confirm returns true and shows a diff", async () => {
    const confirm = vi.fn<Confirm>(async () => true);
    const out = await runTool(
      "write_file",
      { path: "out.txt", content: "new content\n" },
      { cwd, confirm },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "out.txt"), "utf8")).toBe("new content\n");
    expect(confirm).toHaveBeenCalledOnce();
    const ctx = confirm.mock.calls[0]?.[0];
    expect(ctx?.tool).toBe("write_file");
    expect(ctx?.summary).toContain("+new content");
  });

  it("does not write when confirm returns false", async () => {
    const out = await runTool(
      "write_file",
      { path: "out.txt", content: "blocked" },
      { cwd, confirm: deny },
    );
    expect(out).toBe("user declined");
    await expect(readFile(join(cwd, "out.txt"), "utf8")).rejects.toBeTruthy();
  });

  it("errors (not throws) when content arg is not a string", async () => {
    const out = await runTool(
      "write_file",
      { path: "out.txt", content: 123 },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/^error:/);
  });
});

describe("run_command", () => {
  it("runs the command when confirmed and captures output + exit code", async () => {
    const out = await runTool(
      "run_command",
      { command: "echo potluck-ok" },
      { cwd, confirm: allow },
    );
    expect(out).toContain("exit 0");
    expect(out).toContain("potluck-ok");
  });

  it("does not run when declined", async () => {
    const confirm = vi.fn<Confirm>(async () => false);
    const out = await runTool(
      "run_command",
      { command: "echo should-not-run > marker.txt" },
      { cwd, confirm },
    );
    expect(out).toBe("user declined");
    expect(confirm).toHaveBeenCalledOnce();
    await expect(readFile(join(cwd, "marker.txt"), "utf8")).rejects.toBeTruthy();
  });
});

describe("runTool dispatch", () => {
  it("reports unknown tools", async () => {
    const out = await runTool("frobnicate", {}, { cwd, confirm: allow });
    expect(out).toMatch(/unknown tool/);
  });
});
