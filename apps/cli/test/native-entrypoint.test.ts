import { expect, it } from "vitest";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

it.each([false, true])("the installed CLI sends native controls, applies an approved edit, and verifies (bounded=%s)", async bounded => {
  const dir = await mkdtemp(join(tmpdir(), "potluck-native-entry-"));
  const requests: any[] = [];
  const server = createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");
    if (req.url?.includes("/potluck/context?")) {
      res.end(JSON.stringify({ model: "fixture", scope: "local", context_window: 16384 })); return;
    }
    if (req.url?.endsWith("capabilities")) {
      res.end(JSON.stringify({ reasoning_budget_request_version: 1, parallel_tool_calls_request_version: 1 })); return;
    }
    let text = ""; for await (const piece of req) text += piece;
    requests.push(JSON.parse(text));
    const turn = requests.length;
    const action = turn === 1 ? { name: "read_file", arguments: JSON.stringify({ path: "value.txt" }) }
      : { name: "edit_file", arguments: JSON.stringify({ path: "value.txt", old_string: "wrong", new_string: "correct" }) };
    const delta = turn < 3 ? { tool_calls: [{ index: 0, id: `call_${turn}`, type: "function", function: action }] } : { content: "Repaired and verified." };
    res.setHeader("Content-Type", "text/event-stream");
    res.end(`data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason: turn < 3 ? "tool_calls" : "stop" }] })}\n\ndata: [DONE]\n\n`);
  });
  try {
    await writeFile(join(dir, "value.txt"), "wrong\n");
    await writeFile(join(dir, "check.cjs"), 'if (require("node:fs").readFileSync("value.txt", "utf8") !== "correct\\n") process.exit(1);');
    await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
    const env = { ...process.env, POTLUCK_DATA_DIR: join(dir, "data"), POTLUCK_GATEWAY_URL: `http://127.0.0.1:${(server.address() as any).port}/v1`, POTLUCK_GATEWAY_KEY: "test", SNAPSHOT_DIR: join(dir, "snapshots") };
    const result = await promisify(execFile)(process.execPath, [resolve("dist/index.js"), ...(bounded ? ["--bounded-repair"] : []), "--native-tools", "--reasoning-budget", "256", "--model", "fixture", "--scope", "local", "--yes", "--verify", "node check.cjs", "-p", "Correct value.txt without changing other files"], { cwd: dir, env, timeout: 15000 });
    expect(result.stdout).toContain("Repaired and verified");
    expect(result.stderr).toContain("verify passed");
    expect(await readFile(join(dir, "value.txt"), "utf8")).toBe("correct\n");
    expect(requests).toHaveLength(3);
    expect(requests[0].messages.at(-1).content.includes("BOUNDED REPAIR")).toBe(bounded);
    for (const request of requests) expect(request).toMatchObject({ reasoning_budget_tokens: 256, parallel_tool_calls: false, tools: expect.any(Array) });
    expect(requests[1].messages.at(-2)).toMatchObject({ role: "tool", tool_call_id: "call_1" });
    expect(requests[2].messages.at(-2)).toMatchObject({ role: "tool", tool_call_id: "call_2", content: expect.stringContaining("passed") });
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()));
    await rm(dir, { recursive: true, force: true });
  }
});
