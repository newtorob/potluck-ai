import { afterEach, expect, it } from "vitest";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { failureEvidence } from "../src/repair-feedback.js";
import { sourceNavigation } from "../src/source-navigation.js";
import { runTool, runVerify, type FileReadObservation } from "../src/tools.js";
import { SourceContext } from "../src/source-context.js";
import { RepairProgress } from "../src/repair-progress.js";

const dirs: string[] = [];
afterEach(async () => { for (const dir of dirs.splice(0)) await rm(dir, { recursive: true, force: true }); });
async function workspace() { const dir = await mkdtemp(join(tmpdir(), "repair-evidence-")); dirs.push(dir); return dir; }

it("retains literal assertion inputs and differences amid a long log without inventing values", () => {
  const text = "warning\n".repeat(2000) + 'File "src/calc.py", line 88\n    actual = area(3, 7)\n> assert actual == 21\nE AssertionError: assert 9 == 21\n' + "progress\n".repeat(2000) + "1 failed";
  const evidence = failureEvidence(text);
  expect(evidence.excerpt).toContain("area(3, 7)");
  expect(evidence.excerpt).toContain("9 == 21");
  expect(evidence.excerpt.length).toBeLessThanOrEqual(2400);
  expect(evidence.locations).toEqual([{ path: "src/calc.py", line: 88 }]);
  expect(failureEvidence("something broke").excerpt).toBe("something broke");
});

it("extracts observed assertion calls and does not treat traceback paths as commands", () => {
  const e = failureEvidence('File "/outside/secret.py", line 20\n> assert normalize("X") == "x"\nE AssertionError\nExpected: x\nReceived: X');
  expect(e.symbols).toEqual(["normalize"]);
  expect(e.locations[0].path).toBe("/outside/secret.py");
  expect(e.excerpt).toContain("Expected: x");
  expect(e.excerpt).toContain("Received: X");
});

it("preserves early verification failures even after the rolling output buffer overflows", async () => {
  const cwd = await workspace();
  await writeFile(join(cwd, "check.cjs"), 'console.log("AssertionError: expected 21 got 9"); console.log("noise\\n".repeat(100000)); console.log("1 failed"); process.exitCode=1;');
  const result = await runVerify("node check.cjs", cwd);
  expect(result.code).toBe(1); expect(result.output).toContain("expected 21 got 9");
  expect(result.output).toContain("1 failed"); expect(result.output.length).toBeLessThanOrEqual(3000);
});

it("finds same-file callees outside the observed range and labels only actual declarations", () => {
  const text = "def wrapper(x):\n    return normalize(x)\n" + "# spacing\n".repeat(80) + "def normalize(x):\n    return x\n";
  expect(sourceNavigation(text, "return normalize(x)", [])).toEqual([{ name: "normalize", line: 83 }]);
  expect(sourceNavigation(text, "return invented(x)", [])).toEqual([]);
  expect(sourceNavigation("export const normalize = (x) => x;", "normalize(x)", [])).toEqual([{ name: "normalize", line: 1 }]);
});

it("adds bounded navigation without granting full-read write authority or following an external hint", async () => {
  const cwd = await workspace(), readPaths = new Set<string>(); let observation: FileReadObservation | undefined;
  await writeFile(join(cwd, "calc.py"), "def wrapper(x):\n    return normalize(x)\n" + "# spacing\n".repeat(80) + "def normalize(x):\n    return x\n");
  const deps = { cwd, confirm: async () => true, readPaths, repairSymbols: ["normalize", "/outside/secret.py"], onFileRead: (read: FileReadObservation) => { observation = read; } };
  const text = await runTool("read_file", { path: "calc.py", offset: 1, limit: 2 }, deps);
  expect(text).toContain("normalize: read_file offset=83 limit=60");
  expect(observation?.content).not.toContain("def normalize");
  expect(readPaths.size).toBe(0);
  expect(await runTool("write_file", { path: "calc.py", content: "replacement" }, deps)).toMatch(/^error:/);
  expect((await runTool("read_file", { path: "calc.py", limit: 2 }, { ...deps, repairSymbols: undefined }))).not.toContain("Source navigation");
});

it("prioritizes current failure source over a newer unrelated range and invalidates it after edits", () => {
  const sources = new SourceContext("/repo", "repair"), target = { role: "user" as const, content: "target" }, noise = { role: "user" as const, content: "noise" };
  sources.remember({ path: "/repo/calc.py", startLine: 80, content: "def area(w,h):\n    return w*w\n", truncatedLine: false, navigation: [{ name: "area", line: 80 }] }, target);
  sources.remember({ path: "/repo/calc.py", startLine: 200, content: "other\n", truncatedLine: false }, noise);
  sources.focus(failureEvidence('File "/workspace/calc.py", line 81\nassert area(3,7) == 21'));
  expect(sources.priorities()[0]).toBe(target);
  sources.invalidate("/repo/calc.py"); expect(sources.priorities()).toEqual([]);
});

it("distinguishes repeated failure evidence and clears correction guidance after a pass", () => {
  const p = new RepairProgress(12), a = failureEvidence("AssertionError: 9 != 21");
  p.checked(false, a.fingerprint); expect(p.guidance()).toContain("CORRECTION");
  p.checked(false, a.fingerprint); expect(p.guidance()).toContain("same failure evidence");
  p.checked(false, failureEvidence("AssertionError: 10 != 21").fingerprint); expect(p.guidance()).not.toContain("same failure evidence");
  p.checked(true, ""); expect(p.guidance()).not.toContain("CORRECTION");
  p.checked(false, a.fingerprint); p.pendingCheck();
  expect(p.guidance()).not.toContain("CORRECTION");
});
