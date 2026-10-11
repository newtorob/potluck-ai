import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { benchmarkExitCode, benchmarkMachine, formatReport, type BenchmarkDeps, type BenchmarkIO, type BenchmarkReport } from "../src/benchmark.js";
import { runOperation } from "../src/operations.js";
import { Decoy, FakeEngine, gatewayKey, token } from "./benchmark-engine.js";

let engine: FakeEngine;
let ticks = 0;
const deps = (extra: Partial<BenchmarkDeps> = {}): BenchmarkDeps => ({ wait: async () => {}, now: () => 0, clock: () => (ticks += 100), ...extra });
beforeEach(async () => {
  engine = new FakeEngine(); await engine.start(); ticks = 0;
  vi.stubEnv("POTLUCK_DATA_DIR", engine.dir);
});
afterEach(async () => { vi.unstubAllEnvs(); await engine.stop(); });

/** A terminal: questions are answered from the list (null once it runs out, like end of input). */
function terminal(answers: Array<string | null> = [], extra: Partial<BenchmarkIO> = {}) {
  const asked: string[] = [], said: string[] = [];
  const io: BenchmarkIO = { interactive: true, ask: async q => { asked.push(q); return answers.length ? answers.shift()! : null; }, progress: m => { said.push(m); }, ...extra };
  return { io, asked, said, text: () => said.join("") };
}
/** No terminal: nothing may be asked. */
function piped(extra: Partial<BenchmarkIO> = {}) {
  const said: string[] = [];
  const io: BenchmarkIO = { interactive: false, ask: async () => { throw new Error("a question was asked without a terminal"); }, progress: m => { said.push(m); }, ...extra };
  return { io, said };
}
const run = (argv: string[], io: BenchmarkIO, d = deps()) => benchmarkMachine(parseArgs(["benchmark", ...argv]), io, d);
const changes = () => engine.paths().filter(p => /\/(install|load|unload|cancel-download)$/.test(p));

describe("benchmarking the model that is loaded", () => {
  it("measures it without switching anything, counting only the runs after the warm-up", async () => {
    engine.bench = call => FakeEngine.run([3600, 3300, 3100, 3500][call]);
    const report = await run([], piped().io);
    expect(report).toMatchObject({ schemaVersion: 1, status: "complete", model: "big", switch: null, loadedAtEnd: "big", loadedAtEndConfirmed: true });
    expect(engine.benchCalls).toBe(4);
    expect(report.runs).toHaveLength(3);
    expect(report.summary!.generationTokensPerSecond).toEqual({ median: 21, min: 19.7, max: 22.5 });
    expect(changes()).toEqual([]);
    expect(benchmarkExitCode(report)).toBe(0);
  });
  it("prints the machine, where the model runs, and the numbers with their labels", async () => {
    engine.bench = call => FakeEngine.run([3600, 3300, 3100, 3500][call]);
    engine.lastBenchmarks.set("big", { model_id: "big", tokens_per_sec: 18.2, ttft_ms: 410 });
    const text = formatReport(await run([], piped().io));
    expect(text).toBe([
      "This machine: Apple M2 Pro, 16 GiB memory, 8.0 GiB available now",
      "Loaded: big on the GPU (Metal), all 37 layers, 8192-token context",
      "Last benchmark of big before this run: 18.2 tokens/s counted over the whole response, first token 410 ms.",
      "big, 3 runs: 21.0 tokens/s (19.7 to 22.5), first token 300 ms, prompt processing 240 tokens/s",
      "Engine's own figure: 19.4 tokens/s, counted over the whole response including the wait for the first token.", ""].join("\n"));
  });
  it("leaves out what a terminal session already printed, unless the measured model differs", async () => {
    const same = formatReport(await run([], piped().io), true);
    expect(same.split("\n")[0]).toMatch(/^big, 3 runs: /);
    expect(same).not.toContain("This machine:");
    const switched = formatReport(await run(["--model", "small", "--yes"], piped().io), true);
    expect(switched).not.toContain("This machine:");
    expect(switched).toMatch(/^Loaded: small on the GPU/);
  });
  it("honours --runs", async () => {
    expect((await run(["--runs", "5"], piped().io)).runs).toHaveLength(5);
    expect(engine.benchCalls).toBe(6);
  });
  it("fails and changes nothing when no model is loaded and none is named, listing installed IDs", async () => {
    engine.loaded = null;
    await expect(run([], piped().io)).rejects.toThrow(/--model <id>.*installed: big, small/);
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("still measures on an engine without the placement route and says where it runs is unknown", async () => {
    engine.inferenceRoute = false;
    const report = await run([], piped().io);
    expect(report.status).toBe("complete");
    expect(formatReport(report)).toContain("Loaded: big (where it runs is not reported by this engine)");
  });
  it("tells the user when the model runs on the CPU although a GPU is listed", async () => {
    engine.placement = model => ({ used_n_ctx: 4096, offloaded_layers: 0, total_layers: 37, summary: { device_kind: "cpu", device_name: "CPU", backend: "cpu", execution_verified: true, model_id: model } });
    const text = formatReport(await run([], piped().io));
    expect(text).toContain("Loaded: big on the CPU, 4096-token context");
    expect(text).toContain("Note: this model runs on the CPU although the engine lists a GPU (Apple M2 Pro).");
  });
  it.each([["a response as long as its first token", FakeEngine.run(300, 300, 64)], ["a single token", FakeEngine.run(300, 300, 1)]])(
    "reports %s without NaN or Infinity", async (_name, fixed) => {
      engine.bench = () => fixed;
      const report = await run([], piped().io);
      expect(report.summary!.generationTokensPerSecond).toBeNull();
      const text = formatReport(report);
      expect(text).toContain("speed after the first token not measured");
      expect(text + JSON.stringify(report)).not.toMatch(/NaN|Infinity|undefined/);
    });
  it("fails with a plain message when the model writes no text", async () => {
    engine.bench = () => FakeEngine.run(0, 0, 0, 0, null);
    const report = await run([], piped().io);
    expect(report).toMatchObject({ status: "failed", error: { message: expect.stringContaining("returned no text") } });
    expect(benchmarkExitCode(report)).toBe(1);
  });
  it("stops when another client switches models mid-run, and keeps nothing from that run", async () => {
    engine.bench = call => ({ ...FakeEngine.run(3300), ...(call === 2 ? { model_id: "intruder" } : {}) }) as never;
    const report = await run([], piped().io);
    expect(report.status).toBe("failed");
    expect(report.error!.message).toContain("changed to another model");
    expect(report.runs).toHaveLength(1);
  });
  it("does not repeat engine error text", async () => {
    engine.bench = () => { throw new Error("secret traceback from the engine"); };
    const report = await run([], piped().io);
    expect(report.status).toBe("failed");
    expect(JSON.stringify(report)).not.toContain("secret traceback");
  });
});

describe("switching models for the benchmark", () => {
  it("loads another installed model, measures it, and loads the first one again by default", async () => {
    engine.placement = model => model === "small"
      ? { used_n_ctx: 4096, offloaded_layers: 0, total_layers: 30, summary: { device_kind: "cpu", device_name: "CPU", backend: "cpu", execution_verified: true } }
      : { used_n_ctx: 8192, offloaded_layers: 37, total_layers: 37, summary: { device_kind: "gpu", device_name: "Apple M2 Pro", backend: "metal", execution_verified: true } };
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ status: "complete", model: "small", loadedAtEnd: "big", switch: { from: "big", to: "small", outcome: "restored" } });
    expect(changes()).toEqual(["POST /models/small/load", "POST /models/big/load"]);
    // The place reported is the one the measured model ran in, not the one loaded before.
    expect(report.ranOn).toMatchObject({ kind: "cpu", totalLayers: 30 });
    expect(formatReport(report)).toContain("Loaded: small on the CPU, 4096-token context");
    expect(formatReport(report)).toContain("Switched to small for the benchmark and back to big.\nLoaded now: big");
    expect(engine.loaded).toBe("big");
    expect(engine.calls.filter(c => c.path === "/models/small/load")[0].body).toEqual({});
  });
  it("keeps the new model loaded with --keep", async () => {
    const report = await run(["--model", "small", "--yes", "--keep"], piped().io);
    expect(report).toMatchObject({ loadedAtEnd: "small", switch: { outcome: "kept" } });
    expect(changes()).toEqual(["POST /models/small/load"]);
    expect(formatReport(report)).toContain("Kept small loaded; it replaced big.\nLoaded now: small");
  });
  it("downloads a model that is not installed first, shows progress, and only then replaces the loaded one", async () => {
    const { io, said } = terminal([]);
    const report = await run(["--model", "fresh", "--yes"], io);
    expect(report).toMatchObject({ status: "complete", model: "fresh", loadedAtEnd: "big" });
    const paths = engine.paths();
    const firstLoad = paths.indexOf("POST /models/fresh/load");
    expect(paths.indexOf("POST /models/fresh/install")).toBeGreaterThan(-1);
    expect(paths.lastIndexOf("GET /models/fresh/download-progress")).toBeLessThan(firstLoad);
    expect(paths.indexOf("POST /models/fresh/install")).toBeLessThan(paths.indexOf("GET /models/fresh/download-progress"));
    expect(paths.slice(paths.indexOf("POST /models/fresh/install"), firstLoad)).not.toContain("POST /models/big/load");
    expect(said.join("")).toMatch(/Downloading fresh: 50% \(1\.5 GiB \/ 3\.0 GiB\)\n[\s\S]*Verifying model checksum\n[\s\S]*Loading fresh locally/);
    expect(changes().at(-1)).toBe("POST /models/big/load");
  });
  it("refuses a model the engine says does not fit, with its reasons, before changing anything", async () => {
    const error = await run(["--model", "huge", "--yes"], piped().io).catch(e => e as Error);
    expect(error.message).toContain("huge is not recommended for current resources: Needs about 73.5 GiB available RAM");
    expect(error.message).toContain("Nothing was changed");
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("refuses an unknown model", async () => {
    await expect(run(["--model", "nope", "--yes"], piped().io)).rejects.toThrow("Model not found");
    expect(changes()).toEqual([]);
  });
  it("refuses to switch without a terminal and without --yes, saying why", async () => {
    const error = await run(["--model", "small"], piped().io).catch(e => e as Error);
    expect(error.message).toBe("To benchmark small, this command would unload big on this machine and load small. Pass --yes to allow that, or run it in a terminal to confirm. Nothing was changed.");
    const download = await run(["--model", "fresh"], piped().io).catch(e => e as Error);
    expect(download.message).toContain("this command would download 3.0 GiB, unload big on this machine, and load fresh");
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("asks before switching in a terminal, and a no changes nothing", async () => {
    const { io, asked } = terminal(["n"]);
    const report = await run(["--model", "small"], io);
    expect(asked).toEqual(["To benchmark small, this will unload big on this machine and load small. Continue? [y/N] "]);
    expect(report.status).toBe("quit");
    expect(formatReport(report)).toBe("Nothing was changed. No benchmark was run.\n");
    expect(benchmarkExitCode(report)).toBe(1);
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("puts the machine back as it was when nothing was loaded: the benchmark model is unloaded", async () => {
    engine.loaded = null;
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ status: "complete", loadedAtEnd: null, loadedAtEndConfirmed: true, switch: { from: null, to: "small", outcome: "unloaded" } });
    expect(changes()).toEqual(["POST /models/small/load", "POST /models/small/unload"]);
    expect(engine.loaded).toBeNull();
    expect(formatReport(report)).toContain("Unloaded small again, as no model was loaded before.\nLoaded now: no model");
    expect(benchmarkExitCode(report)).toBe(0);
  });
  it("leaves it loaded with --keep when nothing was loaded before", async () => {
    engine.loaded = null;
    const report = await run(["--model", "small", "--yes", "--keep"], piped().io);
    expect(report).toMatchObject({ loadedAtEnd: "small", switch: { from: null, to: "small", outcome: "kept" } });
    expect(changes()).toEqual(["POST /models/small/load"]);
    expect(formatReport(report)).toContain("Kept small loaded. No model was loaded before.\nLoaded now: small");
  });
  it("reports the model still loaded, and exits 1, when the unload fails", async () => {
    engine.loaded = null; engine.failUnload.add("small");
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ status: "complete", loadedAtEnd: "small", loadedAtEndConfirmed: true, switch: { outcome: "unload_failed" } });
    expect(benchmarkExitCode(report)).toBe(1);
    expect(formatReport(report)).toContain("Could not unload small, and no model was loaded before. To unload it yourself: potluck models unload small\nLoaded now: small");
    expect(JSON.stringify(report)).not.toContain("secret traceback");
  });
  it("unloads after a failed measurement too, unless --keep", async () => {
    engine.loaded = null; engine.bench = () => FakeEngine.run(0, 0, 0, 0, null);
    const failed = await run(["--model", "small", "--yes"], piped().io);
    expect(failed).toMatchObject({ status: "failed", loadedAtEnd: null, switch: { outcome: "unloaded" } });
    const kept = await run(["--model", "small", "--yes", "--keep"], piped().io);
    expect(kept).toMatchObject({ status: "failed", loadedAtEnd: "small", switch: { outcome: "kept" } });
  });
  it("does not unload anything when the load failed and nothing was loaded before", async () => {
    engine.loaded = null; engine.failLoad.add("small");
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ status: "failed", loadedAtEnd: null, switch: { outcome: "load_failed" } });
    expect(changes()).toEqual(["POST /models/small/load"]);
  });
  it("will not switch away from a model it could not load again", async () => {
    engine.placement = () => null;
    await expect(run(["--model", "small", "--yes"], piped().io)).rejects.toThrow("big is loaded in a way benchmark cannot switch back to");
    expect(changes()).toEqual([]);
    expect((await run([], piped().io)).status).toBe("complete");
  });
  it("stops before changing anything when the loaded model's ID is one it could not load again", async () => {
    engine.loaded = "org/some model";
    await expect(run(["--model", "small", "--yes"], piped().io)).rejects.toThrow("cannot switch back to. Unload it with potluck models unload first. Nothing was changed.");
    expect(changes()).toEqual([]);
  });
  it("loads the first model again when the new one fails to load, only if the engine no longer holds it", async () => {
    engine.failLoad.add("small");
    const kept = await run(["--model", "small", "--yes"], piped().io);
    expect(kept).toMatchObject({ status: "failed", loadedAtEnd: "big", switch: { outcome: "load_failed" } });
    expect(changes()).toEqual(["POST /models/small/load"]);
    engine.calls.length = 0; engine.releaseOnFailedLoad = true;
    const released = await run(["--model", "small", "--yes"], piped().io);
    expect(released).toMatchObject({ status: "failed", loadedAtEnd: "big", switch: { outcome: "load_failed" } });
    expect(changes()).toEqual(["POST /models/small/load", "POST /models/big/load"]);
    expect(JSON.stringify(released)).not.toContain("secret traceback");
  });
  it("does not start a second load when the load times out", async () => {
    engine.loadDelay = 400;
    const report = await run(["--model", "small", "--yes", "--timeout", "100"], piped().io);
    expect(report).toMatchObject({ status: "failed", switch: { outcome: "load_pending" } });
    expect(report.error!.message).toContain("no second load was started");
    expect(changes()).toEqual(["POST /models/small/load"]);
    expect(formatReport(report)).toContain("it may finish later");
  });
});

describe("choosing in a terminal", () => {
  it("lists installed models, then downloads, then ones that do not fit with the reason", async () => {
    const { io, text } = terminal([""]);
    await run([], io);
    expect(text()).toContain([
      "Installed on this machine:", "  big: 5.0 GiB, loaded", "  small: 2.0 GiB, installed",
      "Can be downloaded:", "  fresh: 3.0 GiB, download",
      "Not possible on this machine right now:", "  huge: 60.0 GiB, download, Needs about 73.5 GiB available RAM and 73.5 GiB total RAM."].join("\n"));
    expect(text()).toMatch(/^This machine: Apple M2 Pro, 16 GiB memory, 8\.0 GiB available now\nLoaded: big on the GPU/);
  });
  it("keeps the loaded model on Enter, without asking to switch", async () => {
    const { io, asked } = terminal([""]);
    const report = await run([], io);
    expect(asked).toEqual(["Model to benchmark [big], or q to quit: "]);
    expect(report).toMatchObject({ status: "complete", model: "big", switch: null });
    expect(changes()).toEqual([]);
  });
  it("picks a typed ID, confirms, and switches back when Enter is pressed at the last question", async () => {
    const { io, asked } = terminal(["small", "y", ""]);
    const report = await run([], io);
    expect(asked).toEqual(["Model to benchmark [big], or q to quit: ", "To benchmark small, this will unload big on this machine and load small. Continue? [y/N] ",
      "Keep small loaded, or switch back to big? [k = keep, s = switch back] (s): "]);
    expect(report.switch!.outcome).toBe("restored"); expect(engine.loaded).toBe("big");
  });
  it("keeps the new model when asked to", async () => {
    const report = await run([], terminal(["small", "yes", "k"]).io);
    expect(report).toMatchObject({ loadedAtEnd: "small", switch: { outcome: "kept" } }); expect(engine.loaded).toBe("small");
  });
  it("does not ask what to keep with --restore, or with --keep", async () => {
    const a = terminal(["small", "y"]); await run(["--restore"], a.io);
    expect(a.asked).toHaveLength(2); expect(engine.loaded).toBe("big");
    const b = terminal(["small", "y"]); await run(["--keep"], b.io);
    expect(b.asked).toHaveLength(2); expect(engine.loaded).toBe("small");
  });
  describe("when nothing was loaded before", () => {
    const keepOrUnload = "Keep small loaded, or unload it? Nothing was loaded before. [k = keep, u = unload] (k): ";
    beforeEach(() => { engine.loaded = null; });
    it("keeps the model on Enter, the default", async () => {
      const { io, asked } = terminal(["small", "y", ""]);
      const report = await run([], io);
      expect(asked[2]).toBe(keepOrUnload);
      expect(report).toMatchObject({ loadedAtEnd: "small", switch: { from: null, outcome: "kept" } }); expect(engine.loaded).toBe("small");
    });
    it("unloads it on u", async () => {
      const report = await run([], terminal(["small", "y", "u"]).io);
      expect(report).toMatchObject({ loadedAtEnd: null, switch: { from: null, outcome: "unloaded" } }); expect(engine.loaded).toBeNull();
    });
    it("keeps it at the end of input", async () => {
      expect((await run([], terminal(["small", "y", null]).io)).loadedAtEnd).toBe("small");
    });
    it("does not ask with --restore or --keep", async () => {
      const a = terminal(["small", "y"]); await run(["--restore"], a.io);
      expect(a.asked).toHaveLength(2); expect(engine.loaded).toBeNull();
      const b = terminal(["small", "y"]); await run(["--keep"], b.io);
      expect(b.asked).toHaveLength(2); expect(engine.loaded).toBe("small");
    });
    it("unloads after Ctrl-C at that question", async () => {
      const abort = new AbortController();
      const answers = ["small", "y"];
      const { io } = terminal([], { signal: abort.signal, ask: async () => { const next = answers.shift(); if (next !== undefined) return next; abort.abort(); return null; } });
      const report = await run([], io);
      expect(report).toMatchObject({ status: "complete", loadedAtEnd: null, switch: { outcome: "unloaded" } }); expect(engine.loaded).toBeNull();
    });
  });
  it("quits on q and changes nothing", async () => {
    const { io } = terminal(["q"]);
    const report = await run([], io);
    expect(report.status).toBe("quit"); expect(report.loadedAtEnd).toBe("big");
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("quits at the end of input", async () => {
    expect((await run([], terminal([null]).io)).status).toBe("quit");
  });
  it("asks again after a model that is not in the list or does not fit", async () => {
    const { io, said } = terminal(["nope", "huge", "q"]);
    expect((await run([], io)).status).toBe("quit");
    expect(said.join("")).toContain("nope is not in the list.");
    expect(said.join("")).toContain("huge is not possible on this machine right now: Needs about 73.5 GiB");
  });
  it("shows the engine's picks for this machine ahead of the full list, with why and what was measured", async () => {
    engine.models[0] = { ...engine.models[0], fitBasis: "measured", checked: "passed",
      measured: { hostGiB: 5.2, gpuGiB: 5.4, contextTokens: 4096, tokensPerSecond: 45, checkedAt: "2026-10-09T10:00:00Z" } };
    engine.models[1] = { ...engine.models[1], fitBasis: "measured", checked: "passed",
      measured: { hostGiB: 2.4, gpuGiB: null, contextTokens: 4096, tokensPerSecond: 12.3, checkedAt: "2026-10-09T10:00:00Z" } };
    engine.recommended = [{ id: "big", basis: "proven_fast", tokensPerSecond: 45 }, { id: "small", basis: "proven_slow", tokensPerSecond: 12.3 },
      { id: "fresh", basis: "estimate", tokensPerSecond: null }];
    const { io, text } = terminal([""]);
    const report = await run([], io);
    expect(text()).toContain([
      "Recommended for this machine:",
      "  big (5.0 GiB, loaded): passed its answer check here, 45.0 tokens/s; needs 5.2 GiB, measured here",
      "  small (2.0 GiB, installed): passed its answer check here, 12.3 tokens/s; needs 2.4 GiB, measured here",
      "  fresh (3.0 GiB, download): not measured on this machine yet",
      "Installed on this machine:"].join("\n"));
    expect(report.recommended).toEqual(engine.recommended);
  });
  it("shows a fresh machine's picks as not measured yet, three at most, and nothing the engine did not list", async () => {
    engine.recommended = [{ id: "fresh", basis: "estimate", tokensPerSecond: null }, { id: "small", basis: "estimate", tokensPerSecond: null },
      { id: "big", basis: "estimate", tokensPerSecond: null }, { id: "huge", basis: "estimate", tokensPerSecond: null },
      { id: "fresh", basis: "estimate", tokensPerSecond: null }, { id: "not-installed-anywhere", basis: "estimate" }, "huge", null];
    const { io, text } = terminal([""]);
    const report = await run([], io);
    const block = text().split("Installed on this machine:")[0].split("Recommended for this machine:\n")[1];
    expect(block.trimEnd().split("\n")).toEqual([
      "  fresh (3.0 GiB, download): not measured on this machine yet", "  small (2.0 GiB, installed): not measured on this machine yet",
      "  big (5.0 GiB, loaded): not measured on this machine yet"]);
    expect(report.recommended.map(p => p.id)).toEqual(["fresh", "small", "big"]);
  });
  it("prints nothing new for an engine that does not send picks, and the report says none", async () => {
    const { io, text } = terminal([""]);
    const report = await run([], io);
    expect(text()).not.toContain("Recommended for this machine");
    expect(report.recommended).toEqual([]);
    expect(JSON.parse(JSON.stringify(report))).toMatchObject({ schemaVersion: 1, status: "complete", model: "big", recommended: [] });
  });
  it("shows a pick without proof wording it does not know, and strips engine text", async () => {
    engine.recommended = [{ id: "small", basis: "from_a_newer_engine\u001b[31m", tokensPerSecond: 20 }];
    const { io, text } = terminal([""]);
    await run([], io);
    expect(text()).toContain("Recommended for this machine:\n  small (2.0 GiB, installed)\n");
    expect(text()).not.toContain("\u001b");
  });
  it("lists the picks only in the questions, not in a piped run", async () => {
    engine.recommended = [{ id: "small", basis: "estimate", tokensPerSecond: null }];
    const { io, said } = piped();
    const report = await run([], io);
    expect(said.join("")).not.toContain("Recommended for this machine");
    expect(report.recommended).toEqual([{ id: "small", basis: "estimate", tokensPerSecond: null }]);
  });
  it("offers the recommended model when nothing is loaded", async () => {
    engine.loaded = null;
    const { io, asked } = terminal(["q"]);
    await run([], io);
    expect(asked[0]).toBe("Model to benchmark [small], or q to quit: ");
  });
});

describe("restoring the model that was loaded before", () => {
  it("says exactly which model is loaded when switching back fails", async () => {
    engine.failLoad.add("big");
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ status: "complete", loadedAtEnd: "small", loadedAtEndConfirmed: true, switch: { from: "big", to: "small", outcome: "restore_failed" } });
    expect(benchmarkExitCode(report)).toBe(1);
    expect(formatReport(report)).toContain("Could not switch back to big. To load it yourself: potluck models load big\nLoaded now: small");
    expect(JSON.stringify(report)).not.toContain("secret traceback");
  });
  it("reports no model when the failed load had also released the new one", async () => {
    engine.failLoad.add("big"); engine.releaseOnFailedLoad = true;
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ loadedAtEnd: null, loadedAtEndConfirmed: true, switch: { outcome: "restore_failed" } });
    expect(formatReport(report)).toContain("Could not switch back to big. To load it yourself: potluck models load big\nLoaded now: no model");
  });
  it("says the state could not be confirmed when the engine cannot be asked", async () => {
    engine.onBench = async call => { if (call === 3) engine.setupFails = true; };
    const report = await run(["--model", "small", "--yes"], piped().io);
    expect(report).toMatchObject({ loadedAtEnd: null, loadedAtEndConfirmed: false, switch: { outcome: "restored" } });
    expect(formatReport(report)).toContain("Loaded now: could not be confirmed. Run potluck status.");
    expect(JSON.stringify(report)).not.toContain("secret traceback");
  });
});

describe("Ctrl-C", () => {
  it("cancels the download this command started, and never loads the new model or unloads the old", async () => {
    const abort = new AbortController();
    const report = await run(["--model", "fresh", "--yes"], piped({ signal: abort.signal }).io, deps({ wait: async () => { abort.abort(); } }));
    expect(report).toMatchObject({ status: "interrupted", loadedAtEnd: "big", switch: null });
    expect(benchmarkExitCode(report)).toBe(130);
    expect(engine.count("/models/fresh/cancel-download")).toBe(1);
    expect(changes()).toEqual(["POST /models/fresh/install", "POST /models/fresh/cancel-download"]);
    expect(engine.loaded).toBe("big");
  });
  it("leaves a download someone else started running", async () => {
    engine.downloads.set("fresh", ["downloading", "complete"]);
    const abort = new AbortController();
    const report = await run(["--model", "fresh", "--yes"], piped({ signal: abort.signal }).io, deps({ wait: async () => { abort.abort(); } }));
    expect(report.status).toBe("interrupted");
    expect(engine.count("/models/fresh/cancel-download")).toBe(0);
    expect(engine.downloads.has("fresh")).toBe(true);
  });
  it("loads the previous model again when interrupted during a measurement, and reports the runs that finished", async () => {
    const abort = new AbortController();
    engine.onBench = async call => { if (call === 2) { abort.abort(); await new Promise(resolve => setTimeout(resolve, 60)); } };
    const report = await run(["--model", "small", "--yes", "--keep"], piped({ signal: abort.signal }).io);
    expect(report).toMatchObject({ status: "interrupted", loadedAtEnd: "big", loadedAtEndConfirmed: true, switch: { outcome: "restored" } });
    expect(report.runs).toHaveLength(1);
    expect(engine.loaded).toBe("big");
    expect(formatReport(report)).toContain("Interrupted. 1 of 3 runs finished.");
    expect(benchmarkExitCode(report)).toBe(130);
  });
  it("unloads the benchmark model when interrupted after a switch from no model", async () => {
    engine.loaded = null;
    const abort = new AbortController();
    engine.onBench = async call => { if (call === 2) { abort.abort(); await new Promise(resolve => setTimeout(resolve, 60)); } };
    const report = await run(["--model", "small", "--yes", "--keep"], piped({ signal: abort.signal }).io);
    expect(report).toMatchObject({ status: "interrupted", loadedAtEnd: null, loadedAtEndConfirmed: true, switch: { from: null, outcome: "unloaded" } });
    expect(changes()).toEqual(["POST /models/small/load", "POST /models/small/unload"]);
    expect(engine.loaded).toBeNull();
    expect(formatReport(report)).toContain("Interrupted. 1 of 3 runs finished.\nUnloaded small again, as no model was loaded before.\nLoaded now: no model");
  });
  it("changes nothing when interrupted at the first question", async () => {
    const abort = new AbortController();
    const { io } = terminal([], { signal: abort.signal, ask: async () => { abort.abort(); return null; } });
    const report = await run([], io);
    expect(report).toMatchObject({ status: "interrupted", loadedAtEnd: "big" });
    expect(formatReport(report, true)).toBe("Interrupted. The model loaded on this machine was not changed.\n");
    expect(changes()).toEqual([]);
  });
  it("loads the previous model again when interrupted while the new one loads", async () => {
    const abort = new AbortController();
    engine.loadDelay = 80;
    const timer = setTimeout(() => abort.abort(), 20);
    const report = await run(["--model", "small", "--yes"], piped({ signal: abort.signal }).io);
    clearTimeout(timer);
    expect(report).toMatchObject({ status: "interrupted", switch: { outcome: "restored" } });
    expect(changes()).toEqual(["POST /models/small/load", "POST /models/big/load"]);
    expect(engine.loaded).toBe("big");
  });
});

describe("--scope mine", () => {
  const row = (id: string, local: boolean, names: string[]) => ({ id, sha256: null, local, idleMachineCount: names.length, machines: names.map(name => ({ name, meshIp: "100.64.0.2", idle: true })) });
  const peers = () => { engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [], models: [
    row("big", true, ["studio"]), row("remote-14b", false, ["studio", "nas"]), row("only-here", true, [])] }; };
  const chatCalls = () => engine.calls.filter(c => c.path === "/v1/chat/completions");

  it("times the models other machines have loaded from this machine, labelled as including the network", async () => {
    peers();
    const report = await run(["--scope", "mine", "--runs", "2"], piped().io);
    expect(report.status).toBe("complete");
    expect(report.mine!.rows).toHaveLength(1);
    // The fake clock advances 100 ms per reading: first token 100 ms after the start, then 100 ms per later chunk.
    expect(report.mine!.rows[0]).toMatchObject({ model: "remote-14b", error: null, offeredBy: ["nas", "studio"],
      firstTokenMs: { median: 100 }, tokensPerSecond: { median: 10 } });
    expect(report.mine!.rows[0].runs).toHaveLength(2);
    expect(chatCalls()).toHaveLength(3); // one warm-up and two timed runs
    const text = formatReport(report);
    expect(text).toContain("Your other machines, measured from this machine over the mesh. These times include the network.");
    expect(text).toContain("remote-14b, 2 runs: 10.0 tokens/s after the first token, first token 100 ms");
    expect(text).toContain("  Answered by one of: nas, studio. The engine does not say which.");
    expect(benchmarkExitCode(report)).toBe(0);
  });
  it("names the machine when only one offers the model, and lists them when several do", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [],
      models: [row("solo", false, ["newtorob"]), row("pair", false, ["a", "b"]), row("unnamed", false, ["\x1b[0m"])] };
    const report = await run(["--scope", "mine", "--runs", "1"], piped().io);
    const text = formatReport(report);
    expect(text).toContain("solo, 1 run: 10.0 tokens/s after the first token, first token 100 ms\n  On newtorob, the only other machine of yours offering this model.");
    expect(text).toContain("pair, 1 run: 10.0 tokens/s after the first token, first token 100 ms\n  Answered by one of: a, b. The engine does not say which.");
    expect(text).toContain("unnamed, 1 run: 10.0 tokens/s after the first token, first token 100 ms\n  The machine that answered is not known.");
    expect(text).not.toContain("Offered by");
    expect(report.mine!.rows.map(r => r.offeredBy)).toEqual([["newtorob"], ["a", "b"], []]);
  });
  it("skips a model that is also loaded here, and says why", async () => {
    peers();
    const report = await run(["--scope", "mine"], piped().io);
    expect(report.mine!.skipped).toEqual([{ model: "big", reason: "also loaded on this machine, so a request for it is answered here and not over the mesh" }]);
    expect(chatCalls().every(c => (c.body as { model: string }).model === "remote-14b")).toBe(true);
    expect(formatReport(report)).toContain("big: skipped, also loaded on this machine");
  });
  it("uses the model loaded after any switch back as the one to skip", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [], models: [row("small", false, ["studio"])] };
    const report = await run(["--model", "small", "--yes", "--scope", "mine"], piped().io);
    // small is loaded here only for the measurement; once big is back, small on the other machine is measurable.
    expect(report.mine!.rows.map(r => r.model)).toEqual(["small"]);
  });
  it("sends the request with the mine scope, the engine's own key and the engine's address, never the configured gateway", async () => {
    peers();
    const decoy = new Decoy(); await decoy.start();
    try {
      vi.stubEnv("POTLUCK_GATEWAY_URL", decoy.url); vi.stubEnv("POTLUCK_GATEWAY_KEY", "sk-from-the-environment");
      await writeFile(join(engine.dir, "cli.json"), JSON.stringify({ gatewayUrl: decoy.url, model: "x" }));
      const report = await run(["--scope", "mine", "--runs", "1"], piped().io);
      expect(decoy.hits).toEqual([]);
      expect(chatCalls().length).toBeGreaterThan(0);
      for (const c of chatCalls()) {
        expect(c.headers.authorization).toBe(`Bearer ${gatewayKey}`);
        expect(c.headers["x-potluck-scope"]).toBe("mine");
      }
      expect(report.mine!.configuredGatewayIgnored).toBe(true);
      expect(formatReport(report)).toContain("The gateway address in your settings was not used; this reads only the Potluck engine on this machine.");
    } finally { await decoy.stop(); }
  });
  it("does not report a configured address as set aside when it is this engine's", async () => {
    peers(); vi.stubEnv("POTLUCK_GATEWAY_URL", `${engine.url}/v1`);
    const report = await run(["--scope", "mine", "--runs", "1"], piped().io);
    expect(report.mine!.configuredGatewayIgnored).toBe(false);
    expect(formatReport(report)).not.toContain("was not used");
  });
  it("refuses an answer the engine did not say came from another machine", async () => {
    peers();
    engine.chat.servedBy = "local";
    const local = await run(["--scope", "mine", "--runs", "1"], piped().io);
    expect(local.mine!.rows[0]).toMatchObject({ error: "This machine answered the request itself, so it was not measured over the mesh.", runs: [] });
    expect(chatCalls()).toHaveLength(1); // stopped at the warm-up
    engine.chat.servedBy = null;
    const foreign = await run(["--scope", "mine", "--runs", "1"], piped().io);
    expect(foreign.mine!.rows[0].error).toBe("The answer did not come from a Potluck engine, so it was not counted.");
    expect(benchmarkExitCode(foreign)).toBe(1);
  });
  it("says so when no model can be reached on another machine, and when the engine refuses the key", async () => {
    peers(); engine.chat.status = 404;
    expect((await run(["--scope", "mine", "--runs", "1"], piped().io)).mine!.rows[0].error).toBe("No other machine of yours is serving this model right now.");
    engine.chat.status = 401;
    expect((await run(["--scope", "mine", "--runs", "1"], piped().io)).mine!.rows[0].error).toContain("rejected its key");
  });
  it("says so when no other machine has a model loaded", async () => {
    const report = await run(["--scope", "mine"], piped().io);
    expect(report.mine).toMatchObject({ rows: [], skipped: [] });
    expect(formatReport(report)).toContain("No other machine of yours has a model loaded right now.");
    expect(benchmarkExitCode(report)).toBe(0);
  });
  describe("with no model loaded on this machine", () => {
    beforeEach(() => { engine.loaded = null; peers(); });
    it("skips the local part and measures the other machines", async () => {
      const report = await run(["--scope", "mine", "--runs", "2"], piped().io);
      expect(report).toMatchObject({ status: "complete", model: null, runs: [], summary: null, switch: null,
        localSkipped: { reason: "no_model_loaded", message: "No model is loaded on this machine, so only your other machines were measured." } });
      expect(report.mine!.rows.map(r => r.model)).toEqual(["remote-14b"]);
      expect(engine.benchCalls).toBe(0); expect(changes()).toEqual([]);
      const text = formatReport(report);
      expect(text).toMatch(/^This machine: Apple M2 Pro, 16 GiB memory, 8\.0 GiB available now\nNo model is loaded on this machine, so only your other machines were measured\.\nYour other machines, measured/);
      expect(text).not.toContain("Loaded:");
      expect(benchmarkExitCode(report)).toBe(0);
    });
    it("exits 1 when no other machine could be measured either", async () => {
      engine.available = null;
      const none = await run(["--scope", "mine"], piped().io);
      expect(none).toMatchObject({ localSkipped: { reason: "no_model_loaded" }, mine: { rows: [] } });
      expect(benchmarkExitCode(none)).toBe(1);
      engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: null, sha256: null }, machines: [], models: [row("remote-14b", false, ["studio"])] };
      engine.chat.status = 404;
      const refused = await run(["--scope", "mine", "--runs", "1"], piped().io);
      expect(refused.mine!.rows[0].error).not.toBeNull();
      expect(benchmarkExitCode(refused)).toBe(1);
    });
    it("keeps the error without --scope mine, and does not skip when a model is named", async () => {
      await expect(run([], piped().io)).rejects.toThrow("No model is loaded on this machine. Pass --model <id>");
      await expect(run(["--scope", "local"], piped().io)).rejects.toThrow("Pass --model <id>");
      const report = await run(["--scope", "mine", "--model", "small", "--yes", "--runs", "1"], piped().io);
      expect(report).toMatchObject({ model: "small", localSkipped: null, switch: { outcome: "unloaded" } });
    });
    it("still needs the local gateway", async () => {
      engine.gateway = { enabled: false, api_key: "" };
      await expect(run(["--scope", "mine"], piped().io)).rejects.toThrow("which is off");
    });
    it("asks as before in a terminal", async () => {
      const { io, asked } = terminal(["q"]);
      expect((await run(["--scope", "mine"], io)).status).toBe("quit");
      expect(asked[0]).toBe("Model to benchmark [small], or q to quit: ");
    });
    it("prints the JSON fields for a skipped local part", async () => {
      let stdout = "";
      await runOperation(parseArgs(["benchmark", "--scope", "mine", "--runs", "1", "--json"]), text => { stdout += text; });
      expect(JSON.parse(stdout)).toMatchObject({ model: null, runs: [], summary: null, localSkipped: { reason: "no_model_loaded" }, mine: { rows: [{ model: "remote-14b" }] } });
    });
  });
  it("fails before changing anything when the local gateway is off", async () => {
    engine.gateway = { enabled: false, api_key: "" };
    await expect(run(["--model", "small", "--yes", "--scope", "mine"], piped().io)).rejects.toThrow("which is off. Run potluck gateway enable");
    expect(changes()).toEqual([]); expect(engine.benchCalls).toBe(0);
  });
  it("skips the mesh part when interrupted during the local part", async () => {
    peers();
    const abort = new AbortController();
    engine.onBench = async call => { if (call === 1) abort.abort(); };
    const report = await run(["--scope", "mine"], piped({ signal: abort.signal }).io);
    expect(report.status).toBe("interrupted"); expect(report.mine).toBeUndefined(); expect(chatCalls()).toHaveLength(0);
  });
  it("keeps the rows already measured when interrupted during the mesh part", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [], models: [row("a-model", false, ["studio"]), row("b-model", false, ["nas"])] };
    const abort = new AbortController();
    const report = await run(["--scope", "mine", "--runs", "1"], piped({ signal: abort.signal }).io, deps({ clock: () => { if ((ticks += 100) > 1300) abort.abort(); return ticks; } }));
    expect(report.status).toBe("interrupted");
    expect(report.mine!.rows.map(r => r.model)).toEqual(["a-model"]);
  });
  it("measures at most eight models", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [],
      models: Array.from({ length: 10 }, (_, i) => row(`m${i}`, false, ["studio"])) };
    const report = await run(["--scope", "mine", "--runs", "1"], piped().io);
    expect(report.mine!.rows).toHaveLength(8); expect(report.mine!.notMeasured).toBe(2);
    expect(formatReport(report)).toContain("2 more models were not measured");
  });
});

describe("the command", () => {
  let stderr = "";
  beforeEach(() => { stderr = ""; vi.spyOn(process.stderr, "write").mockImplementation(chunk => { stderr += String(chunk); return true; }); });
  afterEach(() => vi.restoreAllMocks());
  const exec = async (argv: string[]) => {
    let stdout = ""; const code = await runOperation(parseArgs(argv), text => { stdout += text; });
    return { code, stdout };
  };
  it("prints one JSON object with the machine, model, place, runs, medians and what is loaded at the end", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [],
      models: [{ id: "remote-14b", sha256: null, local: false, idleMachineCount: 1, machines: [{ name: "studio", meshIp: "100.64.0.2", idle: true }] }] };
    const { code, stdout } = await exec(["benchmark", "--model", "small", "--yes", "--scope", "mine", "--runs", "2", "--json"]);
    expect(code).toBe(0);
    expect(stdout.trim().split("\n")).toHaveLength(1);
    const report = JSON.parse(stdout) as BenchmarkReport;
    expect(Object.keys(report).sort()).toEqual(["acceptance", "gpuDevices", "lastBenchmarkBefore", "loadedAtEnd", "loadedAtEndConfirmed", "localSkipped", "machine", "mine", "model", "ranOn", "recommended",
      "requestedRuns", "runs", "schemaVersion", "status", "summary", "switch"]);
    expect(report).toMatchObject({ schemaVersion: 1, status: "complete", model: "small", requestedRuns: 2, loadedAtEnd: "big", switch: { from: "big", to: "small", outcome: "restored" },
      machine: { description: "Apple M2 Pro, 16 GiB memory, 8.0 GiB available now", ramTotalGB: 16, ramAvailableGB: 8 }, ranOn: { kind: "gpu", backend: "Metal", layersOnGpu: 37, totalLayers: 37, contextTokens: 8192 },
      mine: { measuredFrom: "this machine over the mesh, including network time", rows: [{ model: "remote-14b" }] } });
    expect(report.runs[0]).toHaveProperty("generationTokensPerSecond");
    expect(report.summary!.generationTokensPerSecond).toMatchObject({ median: expect.any(Number), min: expect.any(Number), max: expect.any(Number) });
  });
  it("rounds every derived rate and time in the JSON to one decimal", async () => {
    engine.available = { schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: "big", sha256: null }, machines: [],
      models: [{ id: "remote-14b", sha256: null, local: false, idleMachineCount: 1, machines: [{ name: "studio", meshIp: "100.64.0.2", idle: true }] }] };
    engine.bench = () => FakeEngine.run(3333.37, 301.26, 157, 19.43, 241.77);
    engine.lastBenchmarks.set("big", { model_id: "big", tokens_per_sec: 18.2345, ttft_ms: 410.4567 });
    ticks = 0;
    const odd = { ...deps(), clock: () => (ticks += 33.3333) };
    const report = await run(["--scope", "mine", "--runs", "2"], piped().io, odd);
    const stdout = JSON.stringify(report); // what --json prints
    expect(stdout).not.toMatch(/\d\.\d\d/);
    expect(report.runs[0]).toMatchObject({ ttftMs: 301.3, durationMs: 3333.4, generationTokensPerSecond: 51.4, promptTokensPerSecond: 241.8, engineTokensPerSecond: 19.4 });
    expect(report.summary!.generationTokensPerSecond).toEqual({ median: 51.4, min: 51.4, max: 51.4 });
    expect(report.lastBenchmarkBefore).toMatchObject({ tokensPerSecond: 18.2, firstTokenMs: 410.5 });
    expect(report.mine!.rows[0].tokensPerSecond!.median).toBe(Math.round(report.mine!.rows[0].tokensPerSecond!.median * 10) / 10);
  });
  it("never prints engine errors, addresses, tokens or keys, in JSON or text", async () => {
    engine.failLoad.add("big"); engine.placement = m => ({ used_n_ctx: 1, offloaded_layers: 1, total_layers: 1, lib_dir: "/private/secret/lib", summary: { device_kind: "gpu", backend: "metal", device_name: "GPU", model_id: m, execution_verified: true } });
    for (const argv of [["--json"], []]) {
      const { stdout } = await exec(["benchmark", "--model", "small", "--yes", "--scope", "mine", ...argv]);
      for (const secret of [engine.url, "127.0.0.1", token, gatewayKey, "secret traceback", "/private/secret", "Bearer"]) expect(stdout + stderr).not.toContain(secret);
    }
    expect(stderr).toContain("Warming up small...");
  });
  it("has a non-zero exit when the benchmark did not run", async () => {
    engine.loaded = null;
    await expect(exec(["benchmark"])).rejects.toThrow("Pass --model <id>");
    engine.loaded = "big"; engine.bench = () => FakeEngine.run(0, 0, 0, 0, null);
    expect((await exec(["benchmark", "--json"])).code).toBe(1);
  });
  it("prints its usage without contacting the engine", async () => {
    const { code, stdout } = await exec(["benchmark", "--help"]);
    expect(code).toBe(0); expect(stdout).toContain("Usage: potluck benchmark [--model <id>] [--runs 1-10] [--keep | --restore] [--yes] [--scope local|mine] [--json]");
    expect(engine.calls).toEqual([]);
  });
  it("needs a running engine", async () => {
    vi.stubEnv("POTLUCK_DATA_DIR", join(engine.dir, "elsewhere"));
    await expect(exec(["benchmark"])).rejects.toThrow();
  });
});
