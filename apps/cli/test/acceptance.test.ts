/**
 * The engine's answer check as the person at the terminal sees it: `benchmark`, `status`, `setup` and `models test`,
 * against the fake engine and the fixed contract (acceptanceFixture is the object the contract spells out).
 */
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { benchmarkExitCode, benchmarkMachine, formatReport, type BenchmarkDeps, type BenchmarkIO } from "../src/benchmark.js";
import { acceptanceFor, acceptanceLine, parseAcceptance, parseInference, setupAcceptanceLine } from "../src/benchmark-stats.js";
import { ControlError } from "../src/control.js";
import { setupModel } from "../src/model-setup.js";
import { formatModelTest, type ModelTestReport } from "../src/model-test.js";
import { runOperation } from "../src/operations.js";
import { formatStatus } from "../src/status.js";
import type { DiagnosticReport } from "../src/diagnostics.js";
import { acceptanceFixture as accept, FAILED_MESSAGE, FakeEngine, FALLBACK_MESSAGE } from "./benchmark-engine.js";

const FALLBACK = { from: "gpu", to: "cpu", reason: "gpu_wrong_output" };
const onCpu = { device_kind: "cpu", backend: "cpu", device_name: "CPU", driver: null };
const fellBack = (over: Record<string, unknown> = {}) => accept({ verdict: "gpu_wrong_output", fallback: FALLBACK, message: FALLBACK_MESSAGE, ran_on: onCpu, ...over });
const failed = (over: Record<string, unknown> = {}) => accept({ status: "failed", verdict: "model_failed", message: FAILED_MESSAGE,
  checks: [{ id: "arithmetic", passed: false, required: true }, { id: "echo", passed: true, required: false }], ...over });
const PASSED_LINE = "Answers checked on this setup: passed, 2026-10-09";

let engine: FakeEngine;
let ticks = 0;
const deps: BenchmarkDeps = { wait: async () => {}, now: () => 0, clock: () => (ticks += 100) };
beforeEach(async () => {
  engine = new FakeEngine(); await engine.start(); ticks = 0;
  vi.stubEnv("POTLUCK_DATA_DIR", engine.dir);
  vi.stubEnv("POTLUCK_SIDECAR_URL", engine.url);
  vi.stubEnv("POTLUCK_LOCAL_API_SOCKET", join(engine.dir, "absent.sock"));
  vi.stubEnv("POTLUCK_GATEWAY_KEY", ""); vi.stubEnv("POTLUCK_GATEWAY_URL", "");
});
afterEach(async () => { vi.unstubAllEnvs(); await engine.stop(); });

function piped(): { io: BenchmarkIO; said: string[] } {
  const said: string[] = [];
  return { said, io: { interactive: false, ask: async () => { throw new Error("a question was asked without a terminal"); }, progress: m => { said.push(m); } } };
}
function terminal(answers: Array<string | null> = []): { io: BenchmarkIO; text: () => string } {
  const said: string[] = [];
  return { text: () => said.join(""), io: { interactive: true, ask: async () => answers.length ? answers.shift()! : null, progress: m => { said.push(m); } } };
}
const bench = (argv: string[], io: BenchmarkIO) => benchmarkMachine(parseArgs(["benchmark", ...argv]), io, deps);
const benchText = async (argv: string[] = []) => formatReport(await bench(argv, piped().io));
async function exec(argv: string[]) {
  let stdout = ""; const code = await runOperation(parseArgs(argv), text => { stdout += text; });
  return { code, stdout };
}

describe("reading the engine's acceptance object", () => {
  it("keeps the fields of the contract and drops everything else", () => {
    const a = parseAcceptance(accept({ secret: "never-echo", ran_on: { device_kind: "gpu", backend: "cuda", device_name: "RTX", driver: "560.35", token: "never-echo" },
      checks: [{ id: "arithmetic", passed: true, required: true, hint: "never-echo" }] }));
    expect(a).toEqual({ status: "passed", modelId: "big", level: "quick", checkedAt: "2026-10-09T14:32:05.000Z", fromProof: false,
      ranOn: { deviceKind: "gpu", backend: "CUDA", deviceName: "RTX", driver: "560.35" }, checks: [{ id: "arithmetic", passed: true, required: true, skipped: null }],
      verdict: "ok", fallback: null, message: null });
    expect(JSON.stringify(a)).not.toContain("never-echo");
  });
  it.each([undefined, null, "passed", [], {}, { status: "mystery" }, { status: 3 }])("treats %j as no acceptance, never an error", value => {
    expect(parseAcceptance(value)).toBeNull();
    expect(parseInference({ loaded: {}, devices: [], acceptance: value } as never).acceptance).toBeNull();
  });
  it("is null for an engine whose health report has no acceptance key", () => {
    expect(parseInference({ build: {}, devices: [], loaded: null, benchmark: null }).acceptance).toBeNull();
    expect(parseInference(null).acceptance).toBeNull();
  });
  it("strips escape sequences and control characters from the engine's sentence and bounds it", () => {
    const a = parseAcceptance(accept({ status: "failed", message: "\x1b[31mNo good\x1b[0m\x07\n  at all" }))!;
    expect(a.message).toBe("No good at all");
    expect(parseAcceptance(accept({ status: "failed", message: "x".repeat(2000) }))!.message).toHaveLength(300);
    expect(parseAcceptance(accept({ message: "   " }))!.message).toBeNull();
  });
  it("normalises or drops a time it cannot read, and reads odd values as unknown rather than failing", () => {
    expect(parseAcceptance(accept({ checked_at: "not a time" }))!.checkedAt).toBeNull();
    expect(parseAcceptance(accept({ checked_at: 5 }))!.checkedAt).toBeNull();
    const a = parseAcceptance(accept({ level: "huge", verdict: "weird", ran_on: { device_kind: "tpu" }, checks: "none", from_proof: "yes" }))!;
    expect(a).toMatchObject({ level: null, verdict: null, ranOn: { deviceKind: "unknown", backend: null }, checks: [], fromProof: false });
  });
  it("ignores a result for another model than the one being described", () => {
    const a = parseAcceptance(accept({ model_id: "small" }));
    expect(acceptanceFor(a, "big")).toBeNull();
    expect(acceptanceFor(a, "small")).toBe(a);
    expect(acceptanceFor(a, null)).toBe(a);
    expect(acceptanceFor(null, "big")).toBeNull();
  });
});

describe("the line benchmark and status show", () => {
  const line = (over: Record<string, unknown>) => acceptanceLine(parseAcceptance(accept(over)));
  it("says passed with the date, and says so when a stored result was reused", () => {
    expect(line({})).toBe(PASSED_LINE);
    expect(line({ from_proof: true })).toBe(`${PASSED_LINE} (reused from an earlier check)`);
    expect(line({ checked_at: null })).toBe("Answers checked on this setup: passed");
  });
  it("shows the engine's sentence unchanged for a fallback and for a failure", () => {
    expect(line({ verdict: "gpu_wrong_output", fallback: FALLBACK, message: FALLBACK_MESSAGE })).toBe(FALLBACK_MESSAGE);
    expect(line({ status: "failed", verdict: "model_failed", message: FAILED_MESSAGE })).toBe(FAILED_MESSAGE);
  });
  it("shows the fallback sentence even when the engine marks the run on the CPU as failed", () => {
    expect(line({ status: "failed", fallback: FALLBACK, message: FALLBACK_MESSAGE })).toBe(FALLBACK_MESSAGE);
  });
  it("says a check is still running or turned off, and nothing when none ran", () => {
    expect(line({ status: "running" })).toBe("Answers checked on this setup: still running");
    expect(line({ status: "skipped" })).toBe("Answers checked on this setup: no, the check is turned off");
    expect(line({ status: "not_run" })).toBeNull();
    expect(acceptanceLine(null)).toBeNull();
  });
  it("does not word a case itself when the engine sent no sentence", () => {
    expect(line({ status: "failed", message: null })).toBe("Answers checked on this setup: failed");
    expect(line({ fallback: FALLBACK, message: null })).toBe("Answers checked on this setup: moved from the GPU to the CPU");
  });
});

describe("potluck benchmark", () => {
  it("prints the passed line straight after the Loaded line, and puts the object in the JSON under acceptance", async () => {
    engine.acceptance = accept();
    const report = await bench([], piped().io);
    expect(formatReport(report).split("\n").slice(0, 3)).toEqual(["This machine: Apple M2 Pro, 16 GiB memory, 8.0 GiB available now",
      "Loaded: big on the GPU (Metal), all 37 layers, 8192-token context", PASSED_LINE]);
    expect(report.acceptance).toMatchObject({ status: "passed", modelId: "big", fromProof: false, ranOn: { deviceKind: "gpu", backend: "CUDA", driver: "560.35" } });
    const { stdout } = await exec(["benchmark", "--json", "--runs", "1"]);
    expect(JSON.parse(stdout).acceptance).toMatchObject({ status: "passed", checks: [{ id: "arithmetic", passed: true, required: true }, { id: "echo", passed: true, required: false }] });
  });
  it("says when the result came from a stored proof", async () => {
    engine.acceptance = accept({ from_proof: true });
    expect(await benchText()).toContain(`${PASSED_LINE} (reused from an earlier check)\n`);
  });
  it("shows the engine's fallback sentence in place of the note about the CPU, so the two never disagree", async () => {
    engine.placement = model => ({ used_n_ctx: 4096, offloaded_layers: 0, total_layers: 37, summary: { device_kind: "cpu", device_name: "CPU", backend: "cpu", execution_verified: true, model_id: model } });
    engine.acceptance = fellBack();
    const text = await benchText();
    expect(text).toContain(`Loaded: big on the CPU, 4096-token context\n${FALLBACK_MESSAGE}\n`);
    expect(text).not.toContain("Note: this model runs on the CPU");
    expect(text).not.toContain("Answers checked on this setup: passed");
    // Without a fallback the existing note is still there.
    engine.acceptance = null;
    expect(await benchText()).toContain("Note: this model runs on the CPU although the engine lists a GPU (Apple M2 Pro).");
  });
  it("still measures a model whose check failed, shows the engine's sentence, and says in the summary that the answers did not pass", async () => {
    engine.acceptance = failed();
    const report = await bench([], piped().io);
    expect(report.status).toBe("complete"); expect(report.runs).toHaveLength(3); expect(benchmarkExitCode(report)).toBe(0);
    const text = formatReport(report);
    expect(text).toContain(`Loaded: big on the GPU (Metal), all 37 layers, 8192-token context\n${FAILED_MESSAGE}\n`);
    expect(text).toMatch(/big, 3 runs: [\s\S]*\nEngine's own figure: [^\n]*\nThese speeds are for a model whose answers did not pass the check on this setup\.\n$/);
  });
  it("says a check is still running, and picks up the result when it finishes during the runs", async () => {
    engine.acceptance = accept({ status: "running", checked_at: null });
    const early = await bench(["--runs", "1"], piped().io);
    expect(formatReport(early)).toContain("Answers checked on this setup: still running\n");
    expect(formatReport(early)).not.toContain("did not pass");
    engine.acceptance = accept({ status: "running", checked_at: null }); engine.benchCalls = 0;
    engine.onBench = async call => { if (call === 1) engine.acceptance = failed(); };
    const later = await bench(["--runs", "1"], piped().io);
    expect(later.acceptance).toMatchObject({ status: "failed", message: FAILED_MESSAGE });
    expect(formatReport(later)).toContain(`${FAILED_MESSAGE}\n`);
    expect(formatReport(later)).toContain("These speeds are for a model whose answers did not pass");
  });
  it("tells a person in a terminal about a check that finished while it was measuring", async () => {
    engine.acceptance = accept({ status: "running", checked_at: null });
    engine.onBench = async call => { if (call === 1) engine.acceptance = accept(); };
    const { io, text } = terminal([""]);
    await bench(["--runs", "1"], io);
    expect(text()).toContain("Answers checked on this setup: still running\n");
    expect(text()).toMatch(/Run 1 of 1: [^\n]*\n.*Answers checked on this setup: passed, 2026-10-09\n/s);
  });
  it("says the owner turned the check off, and says nothing when no check ran or the engine has none", async () => {
    engine.acceptance = accept({ status: "skipped", checked_at: null });
    expect(await benchText()).toContain("Loaded: big on the GPU (Metal), all 37 layers, 8192-token context\nAnswers checked on this setup: no, the check is turned off\n");
    for (const acceptance of [accept({ status: "not_run", checked_at: null, model_id: null }), null]) {
      engine.acceptance = acceptance;
      const report = await bench([], piped().io);
      expect(formatReport(report)).not.toContain("Answers");
      if (!acceptance) expect(report.acceptance).toBeNull();
    }
  });
  it("shows the line in a terminal session right after the Loaded line", async () => {
    engine.acceptance = accept();
    const { io, text } = terminal([""]);
    await bench(["--runs", "1"], io);
    expect(text()).toMatch(/^This machine: [^\n]*\nLoaded: big on the GPU[^\n]*\nAnswers checked on this setup: passed, 2026-10-09\n/);
  });
  it("describes the check of the model it measured after a switch, not the one loaded before", async () => {
    // The engine still holds big's result when small is read back, so it says nothing about small.
    engine.acceptance = accept({ model_id: "big" });
    const text = formatReport(await bench(["--model", "small", "--yes", "--keep"], piped().io));
    expect(text).toContain("Loaded: small on the GPU");
    expect(text).not.toContain("Answers checked");
    engine.acceptance = accept({ model_id: "small" });
    expect(formatReport(await bench(["--model", "small", "--yes", "--keep"], piped().io))).toContain(PASSED_LINE);
  });
  it("keeps working against an engine that has no health route at all", async () => {
    engine.inferenceRoute = false;
    const report = await bench([], piped().io);
    expect(report).toMatchObject({ status: "complete", acceptance: null });
  });
});

describe("potluck status", () => {
  it("adds one line and one JSON key when the engine reports the check", async () => {
    engine.acceptance = accept();
    const human = await exec(["status"]);
    expect(human.stdout).toContain(`${PASSED_LINE}\n`);
    expect((await exec(["status", "--json"])).stdout).toContain('"acceptance":{"status":"passed"');
  });
  it("shows the engine's sentence for a fallback", async () => {
    engine.acceptance = fellBack();
    expect((await exec(["status"])).stdout).toContain(`${FALLBACK_MESSAGE}\n`);
  });
  it.each([["an older engine with the route but no key", () => { engine.acceptance = null; }], ["an engine without the route", () => { engine.inferenceRoute = false; }]])(
    "shows nothing and no error for %s", async (_name, setup) => {
      setup();
      const human = await exec(["status"]);
      expect(human.stdout).not.toContain("Answers");
      const json = JSON.parse((await exec(["status", "--json"])).stdout);
      expect(json.acceptance).toBeNull();
      expect(json).toMatchObject({ schemaVersion: 1, runtime: { reachable: true, model: "big" } });
    });
  it("shows nothing for a result that is not about the model the runtime has loaded", async () => {
    engine.acceptance = accept({ model_id: "small" });
    expect((await exec(["status"])).stdout).not.toContain("Answers");
    expect(JSON.parse((await exec(["status", "--json"])).stdout).acceptance).toBeNull();
    engine.acceptance = accept({ model_id: "big" });
    expect((await exec(["status"])).stdout).toContain(PASSED_LINE);
  });
  it("puts the line with the local runtime figures", () => {
    const report = { schemaVersion: 1, scope: "local", ready: true, checks: [], runtime: { reachable: true, model: "big", version: "1" },
      mesh: { reachable: false, paired: null, connected: null, peersOnline: null, peersKnown: null }, gateway: { endpoint: "http://localhost/v1", configured: true, reachable: true, models: ["big"] },
      performance: { sampledAt: "now", local: { model: "big", busy: false, contextTokens: 8192, continuousBatch: false, memoryAvailableGiB: 4, benchmarkTokensPerSecond: 20, benchmarkTtftMs: 300 }, usage: null, household: null },
      acceptance: parseAcceptance(accept()) } as DiagnosticReport;
    expect(formatStatus(report)).toMatch(/Local runtime: idle; context 8192; free RAM 4 GiB\.\nAnswers checked on this setup: passed, 2026-10-09\nLast benchmark: /);
  });
});

describe("potluck setup", () => {
  const io = () => { const said: string[] = []; return { said, io: { ask: async () => null, progress: (m: string) => { said.push(m); } } }; };
  const setup = (sink = io()) => setupModel(parseArgs(["setup", "--model", "big", "--no-input"]), sink.io, { wait: async () => {}, now: () => 0 });
  it("keeps requiring a passed reply, and says where the answers were checked before it says Ready", async () => {
    engine.acceptance = accept();
    const sink = io();
    const result = await setup(sink);
    expect(result).toMatchObject({ status: "ready", model: "big", acceptance: { status: "passed", ranOn: { deviceKind: "gpu", backend: "CUDA" } } });
    const text = sink.said.join("");
    expect(text).toMatch(/Checking a real local response\.\.\.\nAnswers checked: passed on the GPU \(CUDA\)\.\nReady: big returned a local response\./);
    expect(engine.calls.filter(c => c.path === "/models/big/test").map(c => c.body)).toEqual([{}]);
  });
  it("says a stored result was reused, and where it ran", async () => {
    engine.acceptance = accept({ from_proof: true, ran_on: onCpu });
    const sink = io(); await setup(sink);
    expect(sink.said.join("")).toContain("Answers checked: passed on the CPU, reused from an earlier check on 2026-10-09.\n");
  });
  it("shows the engine's sentence when it fell back to the CPU", async () => {
    engine.acceptance = fellBack();
    const sink = io(); expect(await setup(sink)).toMatchObject({ status: "ready" });
    expect(sink.said.join("")).toContain(`${FALLBACK_MESSAGE}\nReady:`);
    expect(sink.said.join("")).not.toContain("Answers checked: passed");
  });
  it("prints nothing extra and adds no key against an older engine", async () => {
    const sink = io(); const result = await setup(sink);
    expect(sink.said.join("")).not.toContain("Answers");
    expect(result).not.toHaveProperty("acceptance");
    expect(result).toMatchObject({ status: "ready", model: "big", scope: "local", pinned: false });
  });
  it("fails with the engine's plain sentence when the test is refused, and nothing else from the response", async () => {
    engine.testRefusal = { status: 503, body: { detail: FAILED_MESSAGE, traceback: "secret traceback from the engine", path: "/Users/someone/model.gguf" } };
    const error = await setup().catch(e => e as Error);
    expect(error.message).toBe(FAILED_MESSAGE);
    expect(String(error.stack)).not.toContain("secret traceback");
    expect(JSON.stringify(error)).not.toContain("someone");
  });
  it("keeps the generic message when the refusal has no usable sentence, or is not a refusal of the check", async () => {
    for (const [status, body, message] of [[503, { detail: ["not", "a", "string"] }, "Service returned HTTP 503."], [503, "plain text", "Service returned HTTP 503."],
      [503, { detail: "x".repeat(100000) }, "Service returned HTTP 503."], [500, { detail: "secret traceback from the engine" }, "Service returned HTTP 500."],
      [404, { detail: "secret traceback from the engine" }, "Service returned HTTP 404."]] as const) {
      engine.testRefusal = { status, body };
      const error = await setup().catch(e => e as Error);
      expect(error).toBeInstanceOf(ControlError);
      expect(error.message).toBe(message);
    }
  });
  it("strips control characters from the engine's sentence", async () => {
    engine.testRefusal = { status: 503, body: { detail: "\x1b[31mThe model\x07 gave\nno text.\x1b[0m" } };
    expect((await setup().catch(e => e as Error)).message).toBe("The model gave no text.");
  });
  it("words each case for the person with the helper too", () => {
    expect(setupAcceptanceLine(parseAcceptance(accept({ status: "skipped" })))).toBe("Answers were not checked: the check is turned off.");
    expect(setupAcceptanceLine(parseAcceptance(accept({ status: "running" })))).toBe("Answers check: still running.");
    expect(setupAcceptanceLine(parseAcceptance(accept({ status: "not_run" })))).toBeNull();
    expect(setupAcceptanceLine(parseAcceptance(failed()))).toBe(FAILED_MESSAGE);
    expect(setupAcceptanceLine(null)).toBeNull();
  });
});

describe("potluck models test", () => {
  const testCalls = () => engine.calls.filter(c => c.path.endsWith("/test"));
  it("tests the loaded model with the quick level and no force, and prints each check, where it ran and the driver", async () => {
    engine.acceptance = accept();
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(0);
    expect(stdout).toBe(["big: answer check passed", "Ran now, quick check.", "Ran on: the GPU (CUDA), NVIDIA GeForce RTX 2080 Ti", "Driver: 560.35", "Checks:",
      "  arithmetic: passed, required", "  echo: passed, not required", ""].join("\n"));
    expect(testCalls().map(c => `${c.method} ${c.path} ${JSON.stringify(c.body)}`)).toEqual(['POST /models/big/test {"level":"quick","force":false}']);
  });
  it("sends --full and --force, and tests the model it is given", async () => {
    engine.loaded = "small"; engine.acceptance = accept({ model_id: "small", level: "full" });
    const { code, stdout } = await exec(["models", "test", "small", "--full", "--force"]);
    expect(code).toBe(0); expect(stdout).toContain("Ran now, full check.");
    expect(testCalls().map(c => `${c.path} ${JSON.stringify(c.body)}`)).toEqual(['/models/small/test {"level":"full","force":true}']);
    expect(engine.calls.some(c => c.path === "/models/setup-options")).toBe(false);
  });
  it("says a stored result was reused, and how to run it again", async () => {
    engine.acceptance = accept({ from_proof: true });
    expect((await exec(["models", "test"])).stdout).toContain("Reused an earlier result from 2026-10-09; nothing ran now. Add --force to run it again.\n");
  });
  it("prints a check that did not apply as skipped, with the reason, and never as failed", async () => {
    engine.acceptance = accept({ checks: [{ id: "arithmetic", passed: true, required: true },
      { id: "reasoning", passed: true, required: false, skipped: "not a thinking model" }] });
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(0);
    expect(stdout).toContain("  arithmetic: passed, required\n");
    expect(stdout).toContain("  reasoning: skipped (not a thinking model), not required\n");
    expect(stdout).not.toContain("reasoning: passed");
  });
  it("passes after a fallback to the CPU and shows the engine's sentence", async () => {
    engine.acceptance = fellBack();
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(0);
    expect(stdout).toContain("big: answer check passed\n");
    expect(stdout).toContain("Ran on: the CPU\n");
    expect(stdout.endsWith(`${FALLBACK_MESSAGE}\n`)).toBe(true);
  });
  it("exits 1 when the engine refuses the test, shows its sentence once, and reads the checks it kept", async () => {
    engine.testRefusal = { status: 503, body: { detail: FAILED_MESSAGE, traceback: "secret traceback from the engine" } };
    engine.acceptance = failed();
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(1);
    expect(stdout).toBe(["big: answer check failed", "Ran now, quick check.", "Ran on: the GPU (CUDA), NVIDIA GeForce RTX 2080 Ti", "Driver: 560.35", "Checks:",
      "  arithmetic: failed, required", "  echo: passed, not required", FAILED_MESSAGE, ""].join("\n"));
    expect(stdout).not.toContain("secret traceback");
  });
  it("exits 1 with only the sentence when the engine kept no failing result to read", async () => {
    engine.testRefusal = { status: 503, body: { detail: "The local model returned no text. Check its chat template or context settings." } };
    engine.acceptance = accept();
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(1);
    expect(stdout).toBe("big: answer check failed\nThe local model returned no text. Check its chat template or context settings.\n");
  });
  it("exits 1 when a reply that passed carries a failed acceptance", async () => {
    engine.acceptance = failed();
    const { code, stdout } = await exec(["models", "test"]);
    expect(code).toBe(1); expect(stdout).toContain("big: answer check failed\n"); expect(stdout.endsWith(`${FAILED_MESSAGE}\n`)).toBe(true);
  });
  it("says so when the engine has no answer check, without failing", async () => {
    const { code, stdout } = await exec(["models", "test", "--full"]);
    expect(code).toBe(0);
    expect(stdout).toBe("big: replied to a test prompt\nThis engine does not report an answer check, so only one reply was tested.\n");
  });
  it("reports a check that is turned off or has no result yet in the same plain words", async () => {
    engine.acceptance = accept({ status: "skipped", checked_at: null });
    expect((await exec(["models", "test"])).stdout).toBe("big: replied to a test prompt\nAnswers checked on this setup: no, the check is turned off\n");
    engine.acceptance = accept({ status: "not_run", checked_at: null, model_id: null });
    expect((await exec(["models", "test"])).stdout).toBe("big: replied to a test prompt\nThe engine has no answer check result for this model yet.\n");
  });
  it("asks for a model when none is loaded, and shows the engine's sentence when the named one is not the loaded one", async () => {
    engine.loaded = null;
    await expect(exec(["models", "test"])).rejects.toThrow("No model is loaded to test. Load one with potluck models load <model-id>.");
    expect(testCalls()).toEqual([]);
    engine.loaded = "big";
    await expect(exec(["models", "test", "small"])).rejects.toThrow("Requested model is no longer loaded. Load the requested model and retry.");
  });
  it("prints a JSON object built field by field, with the acceptance object", async () => {
    engine.acceptance = accept({ extra: "never-echo", ran_on: { device_kind: "gpu", backend: "cuda", device_name: "RTX", driver: "560.35", token: "never-echo" } });
    const { code, stdout } = await exec(["models", "test", "--json", "--force"]);
    expect(code).toBe(0); expect(stdout.trim().split("\n")).toHaveLength(1); expect(stdout).not.toContain("never-echo");
    const report = JSON.parse(stdout) as ModelTestReport;
    expect(Object.keys(report).sort()).toEqual(["acceptance", "action", "durationMs", "forced", "level", "message", "model", "outputTokens", "schemaVersion", "status"]);
    expect(report).toMatchObject({ schemaVersion: 1, action: "test", model: "big", status: "passed", level: "quick", forced: true, durationMs: 25, outputTokens: 8, message: null,
      acceptance: { status: "passed", modelId: "big", ranOn: { deviceKind: "gpu", driver: "560.35" } } });
  });
  it("prints a failed run as JSON with the engine's sentence and exit code 1", async () => {
    engine.testRefusal = { status: 503, body: { detail: FAILED_MESSAGE } }; engine.acceptance = failed();
    const { code, stdout } = await exec(["models", "test", "--json"]);
    expect(code).toBe(1);
    expect(JSON.parse(stdout)).toMatchObject({ status: "failed", message: FAILED_MESSAGE, acceptance: { status: "failed", verdict: "model_failed" } });
  });
  it("formats a report that has no acceptance and no message", () => {
    const r: ModelTestReport = { schemaVersion: 1, action: "test", model: "m", status: "failed", level: "quick", forced: false, durationMs: null, outputTokens: null, acceptance: null, message: null };
    expect(formatModelTest(r)).toBe("m: answer check failed");
  });
});

describe("parsing models test", () => {
  it("reads the model, --full and --force, and defaults to a long timeout", () => {
    expect(parseArgs(["models", "test"])).toMatchObject({ command: "models", action: "test", target: undefined, timeout: 120000 });
    expect(parseArgs(["models", "test", "tiny", "--full", "--force", "--json"])).toMatchObject({ target: "tiny", full: true, force: true, json: true });
    expect(parseArgs(["models", "test", "--timeout", "250000"]).timeout).toBe(250000);
  });
  it.each([["models", "list", "--full"], ["models", "load", "tiny", "--force"], ["benchmark", "--force"], ["status", "--full"], ["--full"]])("rejects --full or --force outside models test: %j", (...argv) => {
    expect(() => parseArgs(argv)).toThrow(/--full and --force require models test|require a management command|Unknown/);
  });
  it.each([["models", "test", "../x"], ["models", "test", "a", "b"], ["models", "test", "--full=1"], ["models", "load", "tiny", "--timeout", "250000"]])("rejects %j", (...argv) => {
    expect(() => parseArgs(argv)).toThrow();
  });
  it("keeps the 30 second limit on the other model actions", () => {
    expect(() => parseArgs(["models", "list", "--timeout", "40000"])).toThrow("--timeout must be an integer from 1 to 30000 for this command.");
    expect(parseArgs(["models", "list"]).timeout).toBe(30000);
  });
  it("documents the command in the help", async () => {
    const { stdout } = await exec(["models", "--help"]);
    expect(stdout).toContain("potluck models test [<model-id>] [--full] [--force] [--json]");
  });
});
