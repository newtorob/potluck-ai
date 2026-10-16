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

  it("refuses to overwrite a non-empty file with empty content", async () => {
    await writeFile(join(cwd, "keep.py"), "def f():\n    return 1\n", "utf8");
    const out = await runTool(
      "write_file",
      { path: "keep.py", content: "" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/refusing to overwrite/);
    expect(await readFile(join(cwd, "keep.py"), "utf8")).toBe("def f():\n    return 1\n");
  });

  it("still allows writing empty content to a NEW file", async () => {
    const out = await runTool(
      "write_file",
      { path: "empty.txt", content: "" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/wrote 0 bytes/);
  });

  it("refuses to overwrite an existing non-empty file it hasn't read", async () => {
    await writeFile(join(cwd, "e.py"), "def f():\n    return 1\n", "utf8");
    const readPaths = new Set<string>();
    const out = await runTool(
      "write_file",
      { path: "e.py", content: "def f():\n    return 2\n" },
      { cwd, confirm: allow, readPaths },
    );
    expect(out).toMatch(/without reading it first/);
    expect(await readFile(join(cwd, "e.py"), "utf8")).toBe("def f():\n    return 1\n");
  });

  it("allows overwriting after the file has been read", async () => {
    await writeFile(join(cwd, "e.py"), "def f():\n    return 1\n", "utf8");
    const readPaths = new Set<string>();
    await runTool("read_file", { path: "e.py" }, { cwd, confirm: allow, readPaths });
    const out = await runTool(
      "write_file",
      { path: "e.py", content: "def f():\n    return 2\n" },
      { cwd, confirm: allow, readPaths },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
    expect(await readFile(join(cwd, "e.py"), "utf8")).toBe("def f():\n    return 2\n");
  });

  it("allows creating a NEW file without a prior read", async () => {
    const readPaths = new Set<string>();
    const out = await runTool(
      "write_file",
      { path: "new.py", content: "x = 1\n" },
      { cwd, confirm: allow, readPaths },
    );
    expect(out).toMatch(/wrote \d+ bytes/);
  });
});

describe("run_command", () => {
  it("runs the command when confirmed and captures output + exit code", async () => {
    const started = vi.fn();
    const out = await runTool(
      "run_command",
      { command: "echo potluck-ok" },
      { cwd, confirm: allow, onCommandStarted: started },
    );
    expect(out).toContain("exit 0");
    expect(out).toContain("potluck-ok");
    expect(started).toHaveBeenCalledOnce();
  });

  it("reports execution even when the shell exits nonzero after changing a file", async () => {
    const started = vi.fn();
    await writeFile(join(cwd, "change-and-fail.cjs"),
      'require("node:fs").writeFileSync("marker.txt", "changed"); process.exit(7);');
    const out = await runTool("run_command", { command: "node change-and-fail.cjs" },
      { cwd, confirm: allow, onCommandStarted: started });
    expect(out).toMatch(/^exit 7/);
    expect(await readFile(join(cwd, "marker.txt"), "utf8")).toContain("changed");
    expect(started).toHaveBeenCalledOnce();
  });

  it("does not report execution when the shell fails to spawn", async () => {
    const started = vi.fn();
    const out = await runTool("run_command", { command: "echo unexpected" },
      { cwd: join(cwd, "missing"), confirm: allow, onCommandStarted: started });
    expect(out).toContain("failed to start:");
    expect(started).not.toHaveBeenCalled();
  });

  it("does not report execution for invalid arguments", async () => {
    const started = vi.fn();
    const confirm = vi.fn(allow);
    const out = await runTool("run_command", { command: 123 },
      { cwd, confirm, onCommandStarted: started });
    expect(out).toMatch(/^error:/);
    expect(confirm).not.toHaveBeenCalled();
    expect(started).not.toHaveBeenCalled();
  });

  it("does not run when declined", async () => {
    const confirm = vi.fn<Confirm>(async () => false);
    const started = vi.fn();
    const out = await runTool(
      "run_command",
      { command: "echo should-not-run > marker.txt" },
      { cwd, confirm, onCommandStarted: started },
    );
    expect(out).toBe("user declined");
    expect(confirm).toHaveBeenCalledOnce();
    expect(started).not.toHaveBeenCalled();
    await expect(readFile(join(cwd, "marker.txt"), "utf8")).rejects.toBeTruthy();
  });
});

describe("search_files", () => {
  it("searches an explicit file instead of falsely reporting no matches", async () => {
    await mkdir(join(cwd, "src"));
    await writeFile(join(cwd, "src", "booking.py"), "# booking\ndef is_available():\n    pass\n");
    await writeFile(join(cwd, "other.py"), "def is_available(): pass\n");
    const out = await runTool("search_files", { query: "is_available", path: "src/booking.py" }, { cwd, confirm: allow });
    expect(out).toBe("src/booking.py:2: def is_available():");
  });

  it("reports a missing search path as an error", async () => {
    const out = await runTool("search_files", { query: "needle", path: "missing.py" }, { cwd, confirm: allow });
    expect(out).toMatch(/^error:/);
  });

  it.each(["secret.txt", "build", "build/nested", "build/nested/out.txt", "node_modules/pkg/index.js"])(
    "respects ignore rules for an explicit search path: %s", async (path) => {
      await writeFile(join(cwd, ".gitignore"), "secret.txt\nbuild/\n");
      await mkdir(join(cwd, "build", "nested"), { recursive: true });
      await mkdir(join(cwd, "node_modules", "pkg"), { recursive: true });
      for (const file of ["secret.txt", "build/nested/out.txt", "node_modules/pkg/index.js"]) {
        await writeFile(join(cwd, file), "needle\n");
      }
      const out = await runTool("search_files", { query: "needle", path }, { cwd, confirm: allow });
      expect(out).toMatch(/^error:.*ignored/);
      expect(out).not.toContain(":1:");
    },
  );

  it.each([Buffer.from("needle\0"), Buffer.alloc(2_000_001, "x")])(
    "still skips binary and oversized explicit files", async (content) => {
      await writeFile(join(cwd, "data.bin"), content);
      const out = await runTool("search_files", { query: ".", path: "data.bin" }, { cwd, confirm: allow });
      expect(out).toMatch(/no matches/);
    },
  );

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

  it("replaces every occurrence when replace_all is true", async () => {
    await writeFile(join(cwd, "f.ts"), "dup\ndup\ndup\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "dup", new_string: "x", replace_all: true },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/3 occurrences/);
    expect(await readFile(join(cwd, "f.ts"), "utf8")).toBe("x\nx\nx\n");
  });

  it("the not-unique error points the model at replace_all", async () => {
    await writeFile(join(cwd, "f.ts"), "dup\ndup\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "dup", new_string: "x" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/replace_all/);
  });

  it("matches despite trailing-whitespace drift and notes the fuzzy match", async () => {
    // File has trailing spaces the model won't reproduce.
    await writeFile(join(cwd, "f.ts"), "const a = 1;   \nconst b = 2;\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "f.ts", old_string: "const a = 1;\n", new_string: "const a = 10;\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/edited f\.ts/);
    expect(out).toMatch(/whitespace flexibility/);
    expect(await readFile(join(cwd, "f.ts"), "utf8")).toBe("const a = 10;\nconst b = 2;\n");
  });

  it("matches a UNIFORMLY mis-indented block and re-indents to the file", async () => {
    // File: method body at 8 spaces. Model dedents the whole block.
    await writeFile(join(cwd, "c.py"), "class C:\n    def m(self):\n        return 1\n", "utf8");
    const out = await runTool(
      "edit_file",
      {
        path: "c.py",
        old_string: "def m(self):\n    return 1\n", // dedented (def@0, return@4)
        new_string: "def m(self):\n    return 2\n",
      },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/edited c\.py/);
    // Re-indented back to the file's real indentation (def@4, return@8).
    expect(await readFile(join(cwd, "c.py"), "utf8")).toBe(
      "class C:\n    def m(self):\n        return 2\n",
    );
  });

  it("REJECTS non-uniform indentation drift instead of applying mis-indented code", async () => {
    // File uses 4-space; model's old_string mixes 0-space def + 2-space body.
    await writeFile(join(cwd, "g.py"), "def add(a, b):\n    return a\n", "utf8");
    const out = await runTool(
      "edit_file",
      { path: "g.py", old_string: "def add(a, b):\n  return a\n", new_string: "def add(a, b):\n  return a + b\n" },
      { cwd, confirm: allow },
    );
    expect(out).toMatch(/not found/);
    // File is unchanged - no mis-indented edit was applied.
    expect(await readFile(join(cwd, "g.py"), "utf8")).toBe("def add(a, b):\n    return a\n");
  });
});

describe("runTool dispatch", () => {
  it("reports unknown tools", async () => {
    const out = await runTool("frobnicate", {}, { cwd, confirm: allow });
    expect(out).toMatch(/unknown tool/);
  });
});
