import { chmod, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { runTool } from "../src/tools.js";
import { parseAction, runTask } from "../src/agent.js";
import type { ChatMessage } from "../src/gateway.js";

const stream = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
let cwd: string;
const confirm = vi.fn(async () => true);
beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-quality-"));
  stream.mockReset(); confirm.mockClear();
});

describe("literal file actions", () => {
  const raw = (path: string, content: string, fence = "```") => `${fence}write_file ${JSON.stringify({ path })}\n${content}${fence}\n`;

  it("preserves quotes, backslashes, interpolation and newlines without repairing code", () => {
    const content = `def report(result):\n    return f'Value: {result["value"]}' + r"\\n"\n`;
    expect(parseAction("I'll update the existing function.\n" + raw("report.py", content))).toEqual({
      kind: "action", action: { tool: "write_file", args: { path: "report.py", content } },
    });
  });

  it("keeps nested action fences and JSON as literal file contents", () => {
    const content = '```action\n{"tool":"run_command","args":{"command":"do not run"}}\n```\n';
    expect(parseAction(raw("example.md", content, "````"))).toEqual({
      kind: "action", action: { tool: "write_file", args: { path: "example.md", content } },
    });
  });

  it.each([
    '```write_file {"path":"x"}\nunfinished\n',
    '````write_file {"path":"x"}\ncontent\n```\n',
    '```write_file x.py\ncontent\n```\n',
    '```write_file {"path":null}\ncontent\n```\n',
    '```write_file {"path":"x","content":"ignored"}\ncontent\n```\n',
    '```write_file []\ncontent\n```\n',
  ])("rejects incomplete or malformed raw actions: %s", text => {
    expect(parseAction(text).kind).toBe("error");
  });

  it("never extracts an executable nested action from an unfinished raw block", () => {
    const text = '````write_file {"path":"example.md"}\n```action\n{"tool":"run_command","args":{"command":"wrong"}}\n```\n';
    expect(parseAction(text).kind).toBe("error");
  });

  it("uses an earlier legacy action rather than a subsequent raw write", () => {
    expect(parseAction('```action\n{"tool":"read_file","args":{"path":"x"}}\n```\n' + raw("wrong", "data\n"))).toMatchObject({
      kind: "action", action: { tool: "read_file", args: { path: "x" } },
    });
  });

  it("requires completion and approval before a literal write", async () => {
    let done = false;
    stream.mockImplementationOnce(() => (async function* () { yield raw("new.py", "answer = 42\n"); done = true; })());
    stream.mockImplementation(() => (async function* () { yield "Done."; })());
    const approve = vi.fn(async () => { expect(done).toBe(true); return true; });
    expect(await runTask([], "create", {
      cfg: { baseURL: "unused", apiKey: "test", enabled: true }, model: "pinned", cwd, confirm: approve,
      callbacks: { onToken() {}, onTurnEnd() {}, onAction() {}, onToolResult() {} },
    })).toEqual({ status: "finished" });
    expect(approve).toHaveBeenCalledOnce();
    expect(await readFile(join(cwd, "new.py"), "utf8")).toBe("answer = 42\n");
  });

  it("does not execute a literal write when the transport fails after the closer", async () => {
    stream.mockImplementation(() => (async function* () {
      yield raw("wrong.py", "never = True\n");
      throw new Error("transport failed");
    })());
    await expect(runTask([], "create", {
      cfg: { baseURL: "unused", apiKey: "test", enabled: true }, model: "pinned", cwd, confirm,
      callbacks: { onToken() {}, onTurnEnd() {}, onAction() {}, onToolResult() {} },
    })).rejects.toThrow("transport failed");
    expect(confirm).not.toHaveBeenCalled();
    await expect(readFile(join(cwd, "wrong.py"))).rejects.toMatchObject({ code: "ENOENT" });
  });

  it("still refuses blind overwrites and paths escaping the workspace", async () => {
    await writeFile(join(cwd, "keep.py"), "original\n");
    for (const path of ["keep.py", "../outside.py"]) {
      const action = parseAction(raw(path, "replacement\n"));
      expect(action.kind).toBe("action");
      if (action.kind !== "action") throw new Error("not an action");
      expect(await runTool(action.action.tool, action.action.args, { cwd, confirm, readPaths: new Set() })).toMatch(/^error:/);
    }
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(join(cwd, "keep.py"), "utf8")).toBe("original\n");
  });
});
afterEach(async () => { await rm(cwd, { recursive: true, force: true }); });

describe("current-code repair feedback", () => {
  it("shows the actual placement after a body-line replacement", async () => {
    await writeFile(join(cwd, "format.py"), "def format_value(value):\n    return str(value)\n");
    const out = await runTool("edit_file", {
      path: "format.py", old_string: "return str(value)",
      new_string: "import json\n\ndef format_value(value):\n    return json.dumps(value)",
    }, { cwd, confirm });
    const actual = await readFile(join(cwd, "format.py"), "utf8");
    expect(out).toContain('CURRENT FILE "format.py"');
    expect(out).toContain(actual);
    expect(out).toContain("def format_value(value):\n    import json");
    expect(confirm).toHaveBeenCalledOnce();
  });

  it("returns the real target's contents for a wrong-file edit without redirecting it", async () => {
    await writeFile(join(cwd, "caller.py"), "from worker import transform\n");
    await writeFile(join(cwd, "worker.py"), "def transform():\n    return 0\n");
    const out = await runTool("edit_file", {
      path: "caller.py", old_string: "def transform():\n    return 0", new_string: "def transform():\n    return 1",
    }, { cwd, confirm });
    expect(out).toMatch(/^error:/);
    expect(out).toContain('CURRENT FILE "caller.py"');
    expect(out).toContain("from worker import transform");
    expect(out).toContain("search_files");
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(join(cwd, "worker.py"), "utf8")).toContain("return 0");
  });

  it("grounds identical edits in actual code, not the model's proposed contents", async () => {
    await writeFile(join(cwd, "x.py"), "actual = 0\n");
    const out = await runTool("edit_file", { path: "x.py", old_string: "imagined = 1", new_string: "imagined = 1" }, { cwd, confirm });
    expect(out).toMatch(/^error:/);
    expect(out).toContain("actual = 0");
    expect(out).not.toContain("imagined = 1");
    expect(confirm).not.toHaveBeenCalled();
  });

  it("bounds context around a late edit and marks both omitted ends", async () => {
    const content = "prefix line\n".repeat(2000) + "target = 0\n" + "suffix line\n".repeat(2000);
    await writeFile(join(cwd, "large.py"), content);
    const out = await runTool("edit_file", { path: "large.py", old_string: "target = 0", new_string: "target = 1" }, { cwd, confirm });
    expect(out).toContain("target = 1");
    expect(out).toContain("earlier content omitted");
    expect(out).toContain("later content omitted");
    expect(out.length).toBeLessThan(3400);
  });

  it("never exposes or edits a symlink target outside the workspace", async () => {
    const other = await mkdtemp(join(tmpdir(), "potluck-outside-"));
    try {
      await writeFile(join(other, "private"), "outside-marker");
      await symlink(join(other, "private"), join(cwd, "link"));
      const out = await runTool("edit_file", { path: "link", old_string: "a", new_string: "b" }, { cwd, confirm });
      expect(out).toMatch(/^error:.*escapes/);
      expect(out).not.toContain("outside-marker");
      expect(confirm).not.toHaveBeenCalled();
    } finally { await rm(other, { recursive: true, force: true }); }
  });

  it("keeps declined edits free of success context", async () => {
    await writeFile(join(cwd, "x"), "old");
    const out = await runTool("edit_file", { path: "x", old_string: "old", new_string: "new" }, {
      cwd, confirm: async () => false,
    });
    expect(out).toBe("user declined");
    expect(await readFile(join(cwd, "x"), "utf8")).toBe("old");
  });

  it("does not permit full overwrite after a truncated read, even if an earlier read was complete", async () => {
    const path = join(cwd, "large.py");
    const readPaths = new Set<string>();
    const deps = { cwd, confirm, readPaths };
    await writeFile(path, "short\n");
    await runTool("read_file", { path: "large.py" }, deps);
    const large = "# preserved code\n".repeat(5000);
    await writeFile(path, large);
    const partial = await runTool("read_file", { path: "large.py" }, deps);
    expect(partial).toContain("Partial read");
    expect(partial).toContain("offset=201");
    const out = await runTool("write_file", { path: "large.py", content: "replacement\n" }, deps);
    expect(out).toMatch(/^error: refusing to overwrite/);
    expect(confirm).not.toHaveBeenCalled();
    expect(await readFile(path, "utf8")).toBe(large);
  });

  it.skipIf(process.platform === "win32")("does not treat an unreadable existing file as a new file", async () => {
    const path = join(cwd, "write-only.txt");
    await writeFile(path, "preserve this");
    await chmod(path, 0o200);
    try {
      const out = await runTool("write_file", { path: "write-only.txt", content: "overwrite" }, { cwd, confirm });
      expect(out).toMatch(/^error: cannot read existing file/);
      expect(confirm).not.toHaveBeenCalled();
    } finally { await chmod(path, 0o600); }
    expect(await readFile(path, "utf8")).toBe("preserve this");
  });

  it("retains file identity and the producing action when older history is trimmed", async () => {
    await writeFile(join(cwd, "left.py"), "same-looking code");
    let turn = 0;
    stream.mockImplementation((_cfg, history: ChatMessage[]) => (async function* () {
      if (turn++ === 0) yield '```action\n{"tool":"read_file","args":{"path":"left.py"}}\n```';
      else {
        expect(history.at(-2)?.content).toContain('"path":"left.py"');
        expect(history.at(-2)?.role).toBe("assistant");
        expect(history.at(-1)?.content).toContain('FILE "left.py":\nsame-looking code');
        expect(history.reduce((sum, message) => sum + message.content.length, 0)).toBeLessThanOrEqual(500);
        yield "Inspected.";
      }
    })());
    const history: ChatMessage[] = [
      { role: "system", content: "system" },
      { role: "user", content: "old question".repeat(100) },
      { role: "assistant", content: "old answer" },
    ];
    expect(await runTask(history, "inspect", {
      cfg: { baseURL: "unused", apiKey: "test", enabled: true }, model: "pinned", cwd, confirm,
      charBudget: 500, callbacks: { onToken() {}, onTurnEnd() {}, onAction() {}, onToolResult() {} },
    })).toEqual({ status: "finished" });
  });

  it("keeps the actual request beside repair feedback within the existing history budget", async () => {
    await writeFile(join(cwd, "value.py"), "value = 'old'\n");
    let turn = 0;
    const request = "Keep the specified behavior, including all boundaries.";
    const action = (from: string, to: string) => '```action\n' + JSON.stringify({
      tool: "edit_file", args: { path: "value.py", old_string: from, new_string: to },
    }) + '\n```';
    stream.mockImplementation((_cfg, view: ChatMessage[]) => (async function* () {
      if (turn++ === 0) yield action("'old'", "'incomplete'");
      else if (turn === 2) {
        expect(view.at(-1)?.content).toContain("ORIGINAL REQUEST (still applies):\n" + request);
        expect(view.at(-1)?.content).toContain("missing behavior");
        expect(view.reduce((sum, m) => sum + m.content.length, 0)).toBeLessThan(1200);
        yield action("'incomplete'", "'complete'");
      } else {
        expect(view.at(-1)?.content).not.toContain("ORIGINAL REQUEST");
        yield "Done.";
      }
    })());
    const history: ChatMessage[] = [{ role: "system", content: "system" }];
    const verify = vi.fn(async () => ({ code: (await readFile(join(cwd, "value.py"), "utf8")).includes("'complete'") ? 0 : 1, output: "missing behavior" }));
    expect(await runTask(history, request, {
      cfg: { baseURL: "unused", apiKey: "test", enabled: true }, model: "pinned", cwd, confirm,
      charBudget: 1200, verifyCommand: "fixed", runVerify: verify,
      callbacks: { onToken() {}, onTurnEnd() {}, onAction() {}, onToolResult() {} },
    })).toEqual({ status: "finished" });
    expect(verify).toHaveBeenCalledTimes(2);
    expect(history.some(m => m.content.includes("ORIGINAL REQUEST"))).toBe(false);
  });
});
