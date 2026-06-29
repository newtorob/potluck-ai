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

describe("search_files", () => {
  it("returns file:line: text for matches", async () => {
    await writeFile(join(cwd, "a.ts"), "const x = 1;\nconst hello = 2;\n", "utf8");
    await writeFile(join(cwd, "b.ts"), "no match here\n", "utf8");
    const out = await runTool(
      "search_files",
      { query: "hello" },
      { cwd, confirm: allow },
    );
    expect(out).toContain("a.ts:2:");
    expect(out).toContain("const hello = 2;");
    expect(out).not.toContain("b.ts");
  });

  it("supports regex queries", async () => {
    await writeFile(join(cwd, "a.ts"), "fooBar\nfoobaz\n", "utf8");
    const out = await runTool(
      "search_files",
      { query: "foo[A-Z]\\w+" },
      { cwd, confirm: allow },
    );
    expect(out).toContain("a.ts:1:");
    expect(out).not.toContain("a.ts:2:");
  });

  it("respects .gitignore", async () => {
    await writeFile(join(cwd, ".gitignore"), "secret.txt\nbuild/\n", "utf8");
    await writeFile(join(cwd, "keep.txt"), "needle\n", "utf8");
    await writeFile(join(cwd, "secret.txt"), "needle\n", "utf8");
    await mkdir(join(cwd, "build"));
    await writeFile(join(cwd, "build", "out.txt"), "needle\n", "utf8");

    const out = await runTool(
      "search_files",
      { query: "needle" },
      { cwd, confirm: allow },
    );
    expect(out).toContain("keep.txt");
    expect(out).not.toContain("secret.txt");
    expect(out).not.toContain("build/");
  });

  it("caps results and reports truncation", async () => {
    const lines = Array.from({ length: 150 }, () => "match").join("\n");
    await writeFile(join(cwd, "big.txt"), `${lines}\n`, "utf8");
    const out = await runTool(
      "search_files",
      { query: "match" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/truncated at 100 matches/);
    const hitLines = out.split("\n").filter((l) => l.startsWith("big.txt:"));
    expect(hitLines.length).toBe(100);
  });

  it("reports when there are no matches", async () => {
    await writeFile(join(cwd, "a.txt"), "nothing\n", "utf8");
    const out = await runTool(
      "search_files",
      { query: "absent" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/no matches/);
  });
});

describe("glob", () => {
  it("finds files by ** pattern", async () => {
    await mkdir(join(cwd, "src"));
    await writeFile(join(cwd, "src", "a.ts"), "", "utf8");
    await writeFile(join(cwd, "src", "b.js"), "", "utf8");
    await writeFile(join(cwd, "top.ts"), "", "utf8");

    const out = await runTool("glob", { pattern: "**/*.ts" }, { cwd, confirm: allow });
    const files = out.split("\n");
    expect(files).toContain("src/a.ts");
    expect(files).toContain("top.ts");
    expect(files).not.toContain("src/b.js");
  });

  it("respects .gitignore and reports no matches", async () => {
    await writeFile(join(cwd, ".gitignore"), "*.log\n", "utf8");
    await writeFile(join(cwd, "x.log"), "", "utf8");
    const out = await runTool("glob", { pattern: "**/*.log" }, { cwd, confirm: allow });
    expect(out).toMatch(/no files match/);
  });
});

describe("edit_file", () => {
  it("replaces a unique old_string and shows a diff", async () => {
    await writeFile(join(cwd, "f.ts"), "const a = 1;\nconst b = 2;\n", "utf8");
    const confirm = vi.fn<Confirm>(async () => true);
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "const b = 2;", new_string: "const b = 3;" },
      { cwd, confirm },
    );
    expect(out).toMatch(/^edited f\.ts/);
    expect(await readFile(join(cwd, "f.ts"), "utf8")).toBe("const a = 1;\nconst b = 3;\n");
    const ctx = confirm.mock.calls[0]?.[0];
    expect(ctx?.tool).toBe("edit_file");
    expect(ctx?.summary).toContain("-const b = 2;");
    expect(ctx?.summary).toContain("+const b = 3;");
  });

  it("errors when old_string is absent", async () => {
    await writeFile(join(cwd, "f.ts"), "hello\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "missing", new_string: "x" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/old_string not found/);
  });

  it("errors when old_string is not unique", async () => {
    await writeFile(join(cwd, "f.ts"), "dup\ndup\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "dup", new_string: "x" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/not unique/);
  });

  it("errors when the file does not exist (use write_file)", async () => {
    const out = await runTool(
      "edit_file",
      { path: "nope.ts", old_string: "a", new_string: "b" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/does not exist/);
  });

  it("does not write when declined", async () => {
    await writeFile(join(cwd, "f.ts"), "keep\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "keep", new_string: "changed" },
      { cwd, confirm: deny },
    );
    expect(out).toBe("user declined");
    expect(await readFile(join(cwd, "f.ts"), "utf8")).toBe("keep\n");
  });
});

describe("runTool dispatch", () => {
  it("reports unknown tools", async () => {
    const out = await runTool("frobnicate", {}, { cwd, confirm: allow });
    expect(out).toMatch(/unknown tool/);
  });
});
