import { mkdtemp, open, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { runTool, type Confirm } from "../src/tools.js";

let cwd = "";
const confirm = vi.fn<Confirm>(async () => true);

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-read-range-"));
  confirm.mockClear();
});

afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
});

const body = (output: string): string => output.slice(output.indexOf("\n") + 1);

describe("bounded read_file", () => {
  it("defaults to 200 lines and allows the next range to reach the actual tail", async () => {
    const lines = Array.from({ length: 205 }, (_, i) => `line ${i + 1}\n`);
    await writeFile(join(cwd, "many.txt"), lines.join(""));
    const first = await runTool("read_file", { path: "many.txt" }, { cwd, confirm });
    expect(first).toContain("Partial read: lines 1-200.");
    expect(first).toContain("offset=201 and limit=200");
    expect(body(first)).toBe(lines.slice(0, 200).join(""));
    const tail = await runTool("read_file", { path: "many.txt", offset: 201 }, { cwd, confirm });
    expect(tail).toContain("lines 201-205. End of file.");
    expect(body(tail)).toBe(lines.slice(200).join(""));
  });

  it("uses 1-based offsets and counts CRLF and blank lines without changing the text", async () => {
    await writeFile(join(cwd, "windows.txt"), "one\r\n\r\nthree\r\nfour");
    const range = await runTool("read_file", { path: "windows.txt", offset: 2, limit: 2 }, { cwd, confirm });
    expect(range).toContain("lines 2-3.");
    expect(range).toContain("offset=4 and limit=2");
    expect(body(range)).toBe("\r\nthree\r\n");
    const tail = await runTool("read_file", { path: "windows.txt", offset: 4, limit: 1 }, { cwd, confirm });
    expect(tail).toContain("End of file.");
    expect(body(tail)).toBe("four");
  });

  it.each(["", "only line", "only line\n", "\n", "\r\n", "one\ntwo\n"])(
    "returns a complete file unchanged, including its final newline: %j", async (text) => {
      await writeFile(join(cwd, "exact.txt"), text);
      const readPaths = new Set<string>();
      const output = await runTool("read_file", { path: "exact.txt", offset: 1, limit: 2 }, { cwd, confirm, readPaths });
      expect(output).toBe(text);
      expect(readPaths.has(await realpath(join(cwd, "exact.txt")))).toBe(true);
    },
  );

  it.each(["", "one", "one\n"])("reports an offset after EOF without inventing an empty line: %j", async (text) => {
    await writeFile(join(cwd, "short.txt"), text);
    const output = await runTool("read_file", { path: "short.txt", offset: 2 }, { cwd, confirm });
    expect(output).toMatch(/^error: offset 2 is past end of file \([01] lines\)/);
  });

  it.each([
    ["offset", 0], ["offset", -1], ["offset", 1.5], ["offset", "2"],
    ["offset", null], ["offset", true], ["offset", Infinity], ["offset", NaN],
    ["offset", Number.MAX_SAFE_INTEGER + 1],
    ["limit", 0], ["limit", -1], ["limit", 2.5], ["limit", "2"],
    ["limit", null], ["limit", false], ["limit", Infinity], ["limit", 2001],
  ])("rejects invalid %s=%s instead of coercing it", async (key, value) => {
    await writeFile(join(cwd, "file.txt"), "data");
    const output = await runTool("read_file", { path: "file.txt", [key]: value }, { cwd, confirm });
    expect(output).toMatch(/^error: argument "(offset|limit)" must be an integer from 1 to/);
  });

  it("accepts the maximum line count without adding a phantom line after the final newline", async () => {
    const text = "x\n".repeat(2000);
    await writeFile(join(cwd, "max.txt"), text);
    expect(await runTool("read_file", { path: "max.txt", limit: 2000 }, { cwd, confirm })).toBe(text);
  });

  it("stops before a line that does not fit and continues without losing characters", async () => {
    const first = `${"a".repeat(9999)}\n`;
    const second = `${"b".repeat(7000)}\r\n`;
    await writeFile(join(cwd, "wide.txt"), first + second);
    const start = await runTool("read_file", { path: "wide.txt" }, { cwd, confirm });
    expect(start).toContain("lines 1-1.");
    expect(start).toContain("offset=2");
    expect(body(start)).toBe(first);
    const rest = await runTool("read_file", { path: "wide.txt", offset: 2 }, { cwd, confirm });
    expect(body(rest)).toBe(second);
  });

  it.each([
    ["a".repeat(15999) + "💡tail", "a".repeat(15999)],
    ["a".repeat(15999) + "\r\nnext", "a".repeat(15999)],
    ["漢".repeat(8000) + "💡".repeat(5000), "漢".repeat(8000) + "💡".repeat(4000)],
  ])("caps a single oversized line without splitting Unicode or CRLF", async (text, prefix) => {
    await writeFile(join(cwd, "long.txt"), text);
    const output = await runTool("read_file", { path: "long.txt" }, { cwd, confirm });
    expect(output).toContain("Line 1 is truncated");
    expect(output).toContain("bounded character range");
    expect(body(output)).toBe(prefix);
    expect(output.length).toBeLessThan(16500);
  });

  it("reads later lines after a multi-chunk long line without retaining that line", async () => {
    await writeFile(join(cwd, "large.txt"), "💡".repeat(100_000) + "\r\nlast\n");
    const output = await runTool("read_file", { path: "large.txt", offset: 2, limit: 1 }, { cwd, confirm });
    expect(output).toContain("lines 2-2. End of file.");
    expect(body(output)).toBe("last\n");
  });

  it("does not read an entire giant sparse file to return the first bounded excerpt", async () => {
    const file = await open(join(cwd, "sparse.txt"), "w");
    try {
      await file.write("x".repeat(32_000));
      await file.truncate(1_000_000_000);
    } finally {
      await file.close();
    }
    const output = await runTool("read_file", { path: "sparse.txt" }, { cwd, confirm });
    expect(output).toContain("Line 1 is truncated");
    expect(body(output)).toBe("x".repeat(16000));
  });

  it("keeps an exactly capped file complete", async () => {
    const text = "x".repeat(16000);
    await writeFile(join(cwd, "exact-cap.txt"), text);
    const readPaths = new Set<string>();
    expect(await runTool("read_file", { path: "exact-cap.txt" }, { cwd, confirm, readPaths })).toBe(text);
    expect(readPaths.has(await realpath(join(cwd, "exact-cap.txt")))).toBe(true);
  });

  it("does not turn repeated partial ranges into permission to replace the whole file", async () => {
    const text = "first\nsecond\n";
    await writeFile(join(cwd, "keep.txt"), text);
    const readPaths = new Set<string>();
    for (const offset of [1, 2, 1]) {
      await runTool("read_file", { path: "keep.txt", offset, limit: 1 }, { cwd, confirm, readPaths });
    }
    const result = await runTool("write_file", { path: "keep.txt", content: "replacement\n" }, { cwd, confirm, readPaths });
    expect(result).toMatch(/without reading it first/);
    expect(await readFile(join(cwd, "keep.txt"), "utf8")).toBe(text);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("revokes a previous full-read permission after partial or past-EOF inspection", async () => {
    const text = "first\nsecond\n";
    await writeFile(join(cwd, "keep.txt"), text);
    const readPaths = new Set<string>();
    for (const offset of [2, 3]) {
      await runTool("read_file", { path: "keep.txt" }, { cwd, confirm, readPaths });
      expect(readPaths.size).toBe(1);
      await runTool("read_file", { path: "keep.txt", offset }, { cwd, confirm, readPaths });
      expect(readPaths.size).toBe(0);
    }
  });

  it("refuses whole-file replacement after a character-truncated read but allows a targeted edit", async () => {
    const text = "x".repeat(16001) + "\nlast\n";
    await writeFile(join(cwd, "keep.txt"), text);
    const readPaths = new Set<string>();
    await runTool("read_file", { path: "keep.txt" }, { cwd, confirm, readPaths });
    const overwrite = await runTool("write_file", { path: "keep.txt", content: "replacement\n" }, { cwd, confirm, readPaths });
    expect(overwrite).toMatch(/without reading it first/);
    await runTool("read_file", { path: "keep.txt", offset: 2 }, { cwd, confirm, readPaths });
    const edit = await runTool("edit_file", { path: "keep.txt", old_string: "last", new_string: "changed" }, { cwd, confirm, readPaths });
    expect(edit).toMatch(/edited/);
    expect(await readFile(join(cwd, "keep.txt"), "utf8")).toBe(text.replace("last", "changed"));
    expect(confirm).toHaveBeenCalledOnce();
  });
});
