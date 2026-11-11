import { beforeEach, describe, expect, it, vi } from "vitest";
import { runTask, type AgentOptions } from "../src/agent.js";

const stream = vi.fn();
const runTool = vi.fn();
vi.mock("../src/gateway.js", () => ({ streamChat: (...args: unknown[]) => stream(...args) }));
vi.mock("../src/tools.js", async importOriginal => ({ ...await importOriginal<typeof import("../src/tools.js")>(), runTool: (...args: unknown[]) => runTool(...args) }));

const response = (text: string) => stream.mockImplementation(() => (async function* () { yield text; })());
const options = (): AgentOptions => ({
  cfg: { baseURL: "http://localhost", apiKey: "test", enabled: true }, model: "test", cwd: ".", readOnly: true,
  toolsDisabled: true, maxSteps: 1, contextWindowTokens: 8192, maxOutputTokens: 700,
  confirm: vi.fn(), callbacks: { onToken: vi.fn(), onTurnEnd: vi.fn(), onAction: vi.fn(), onToolResult: vi.fn() },
});
beforeEach(() => { stream.mockReset(); runTool.mockReset(); });
describe("hard no-tools mode", () => {
  it.each([
    ["read_file", { path: "secret" }], ["search_files", { query: "secret" }],
    ["write_file", { path: "x", content: "y" }], ["run_command", { command: "echo x" }],
  ])("rejects %s before action or tool callbacks", async (tool, args) => {
    response(`\`\`\`action\n${JSON.stringify({ tool, args })}\n\`\`\``);
    const opts = options();
    expect(await runTask([], "Review", opts)).toEqual({ status: "incomplete", reason: "tools_disabled" });
    expect(runTool).not.toHaveBeenCalled(); expect(opts.confirm).not.toHaveBeenCalled();
    expect(opts.callbacks.onAction).not.toHaveBeenCalled(); expect(opts.callbacks.onToolResult).not.toHaveBeenCalled();
  });
  it("accepts a final structured response", async () => {
    response('{"status":"complete"}'); const opts = options(); opts.responseFormat = { type: "json_object", schema: { type: "object" } };
    expect(await runTask([], "Review", opts)).toEqual({ status: "finished" }); expect(runTool).not.toHaveBeenCalled();
  });
  it.each([{ readOnly: false }, { verifyCommand: "test" }])("rejects unsafe options before inference", async override => {
    const opts = { ...options(), ...override };
    await expect(runTask([], "Review", opts)).rejects.toThrow(); expect(stream).not.toHaveBeenCalled();
  });
});
