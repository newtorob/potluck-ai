import { execFile } from "node:child_process";
import { cp, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";

import {
  assertBrokenBaseline, checkArguments, checkerScript, checkResult, fingerprint, gradeTrial,
  hashes, inspectWorkspace, markNotRun, planTrials, summarizeTrials, trialCount,
  trialMetrics, validateEnvironment,
} from "../scripts/eval-repo-edits-lib.mjs";

const exec = promisify(execFile);
const cli = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporary: string[] = [];
async function workspace() {
  const cwd = await mkdtemp(join(tmpdir(), "potluck-eval-harness-"));
  temporary.push(cwd);
  return cwd;
}
afterEach(async () => { for (const dir of temporary.splice(0)) await rm(dir, { recursive: true, force: true }); });

const editable = ["left.py", "right.py"];
const before = { "left.py": "a", "right.py": "b", "tests/test_public.py": "test", "potluck.lock": "pin" };
const after = { fileHashes: { ...before, "left.py": "new-a", "right.py": "new-b" }, emptyOrMissingSources: [] };
const passing = () => ({
  agentResult: { status: "finished" }, visible: { code: 0 }, oracle: { code: 0 },
  events: [{ type: "verify", ok: true, elapsedMs: 100 }],
});

describe("evaluation grading", () => {
  it("accepts a finished, independently correct repair despite informational context notices", () => {
    const trial = passing();
    trial.events.push({ type: "notice", message: "Earlier context was trimmed." } as never);
    expect(gradeTrial(trial, before, after, editable)).toMatchObject({ passed: true, failureReasons: [] });
  });

  it.each(["context_limit", "step_limit", "verification_failed", "invalid_action"])(
    "retains an incomplete %s outcome even when final checks happen to pass", reason => {
      const trial = { ...passing(), agentResult: { status: "incomplete", reason } };
      expect(gradeTrial(trial, before, after, editable)).toMatchObject({ passed: false, failureReasons: [`agent_${reason}`] });
    },
  );

  it("does not substitute visible success or prose for independent checks", () => {
    expect(gradeTrial({ ...passing(), oracle: { code: 1 } }, before, after, editable))
      .toMatchObject({ passed: false, failureReasons: ["independent_checks_failed"] });
    expect(gradeTrial({ ...passing(), agentResult: undefined }, before, after, editable).passed).toBe(false);
    expect(gradeTrial({ ...passing(), events: [] }, before, after, editable).passed).toBe(false);
  });

  it.each(["tests/test_public.py", "potluck.lock"])("detects edits to protected %s", name => {
    const changed = { ...after, fileHashes: { ...after.fileHashes, [name]: "tampered" } };
    expect(gradeTrial(passing(), before, changed, editable))
      .toMatchObject({ passed: false, protectedFilesIntact: false, protectedChanges: [name] });
  });

  it("rejects removed tests, new files, empty sources, and incomplete cross-file repairs", () => {
    const deleted = structuredClone(after);
    delete deleted.fileHashes["tests/test_public.py"];
    expect(gradeTrial(passing(), before, deleted, editable).passed).toBe(false);
    const added = { ...after, fileHashes: { ...after.fileHashes, "new.py": "new" } };
    expect(gradeTrial(passing(), before, added, editable).unexpectedFiles).toEqual(["new.py"]);
    const emptied = { ...after, emptyOrMissingSources: ["left.py"] };
    expect(gradeTrial(passing(), before, emptied, editable).failureReasons).toContain("source_deleted_or_emptied");
    expect(gradeTrial(passing(), before, { ...after, fileHashes: { ...after.fileHashes, "right.py": "b" } }, editable).passed).toBe(false);
  });

  it("counts all scheduled attempts, including unrun attempts after transport failure", () => {
    const planned = planTrials([{ id: "one" }, { id: "two" }], 3);
    expect(planned.map(trial => trial.id)).toEqual(["one-1", "two-1", "one-2", "two-2", "one-3", "two-3"]);
    Object.assign(planned[0], { status: "passed", passed: true, metrics: { timeToVerifiedResultMs: 200 } });
    Object.assign(planned[1], { status: "failed", error: "stream disconnected" });
    markNotRun(planned, "transport requires review");
    expect(planned[2].notRunReason).toBe("transport requires review");
    expect(summarizeTrials(planned)).toMatchObject({ planned: 6, passed: 1, failed: 1, notRun: 4, passRate: 1 / 6,
      cases: { one: { verifiedTimesMs: [200, null, null] }, two: { verifiedTimesMs: [null, null, null] } } });
  });

  it("separates retries and first visible success from final independent verification time", () => {
    const trial = { passed: true, agentStartedMs: 50, events: [
      { type: "firstToken", elapsedMs: 100 },
      { type: "verify", ok: false, elapsedMs: 200 },
      { type: "verify", ok: true, elapsedMs: 300 },
      { type: "toolResult", result: "error: wrong match" },
      { type: "confirmation", approved: false },
    ] };
    expect(trialMetrics(trial, 400)).toEqual({
      verificationAttempts: 2, verificationRetries: 1, failedVerifications: 1, toolFailures: 1, deniedActions: 1,
      firstTokenMs: 50, firstVisibleVerifyPassMs: 250, timeToVerifiedResultMs: 350,
    });
    expect(trialMetrics({ ...trial, passed: false }, 400).timeToVerifiedResultMs).toBeNull();
    expect(trialMetrics(trial, 400, { selfTest: true }).timeToVerifiedResultMs).toBeNull();
  });

  it("reports an in-progress trial as unverified rather than already failed", () => {
    const planned = planTrials([{ id: "one" }], 3);
    planned[0].status = "running";
    expect(summarizeTrials(planned)).toMatchObject({ planned: 3, passed: 0, failed: 0, notRun: 2, running: 1, passRate: 0 });
  });
});

describe("evaluation metadata", () => {
  it("defaults to three trials, one for references, and bounds requested repetitions", () => {
    expect(trialCount(undefined)).toBe(3);
    expect(trialCount(undefined, true)).toBe(1);
    expect(trialCount("20")).toBe(20);
    for (const value of ["0", "-1", "3.5", "21", "1x", "", "NaN"]) expect(() => trialCount(value)).toThrow();
  });

  it("requires explicit runtime/backend/hardware conditions and rejects arbitrary extra fields", () => {
    const environment = { schemaVersion: 1, runtimeVersion: "test-version", backend: { name: "llama.cpp", version: "exact-build" },
      hardware: ["CPU model, RAM, GPU model and VRAM"], placement: "single machine", transport: "none" };
    expect(validateEnvironment(environment)).toEqual(environment);
    expect(() => validateEnvironment({ ...environment, backend: { name: "guessed" } })).toThrow();
    expect(() => validateEnvironment({ ...environment, hardware: [] })).toThrow();
    expect(() => validateEnvironment({ ...environment, apiKey: "never store credentials" })).toThrow();
  });

  it("hashes fixture bytes and detects missing/empty sources without accepting symlinks", async () => {
    const cwd = await workspace();
    await writeFile(join(cwd, "left.py"), "left = 1\n");
    await writeFile(join(cwd, "right.py"), "right = 2\n");
    const original = await hashes(cwd);
    expect(fingerprint(original)).toBe(fingerprint(await hashes(cwd)));
    await writeFile(join(cwd, "left.py"), " \n");
    expect(fingerprint(await hashes(cwd))).not.toBe(fingerprint(original));
    expect((await inspectWorkspace(cwd, editable)).emptyOrMissingSources).toEqual(["left.py"]);
    await rm(join(cwd, "right.py"));
    expect((await inspectWorkspace(cwd, editable)).emptyOrMissingSources).toEqual(editable);
    if (process.platform !== "win32") {
      await symlink("left.py", join(cwd, "alias.py"));
      await expect(hashes(cwd)).rejects.toThrow("non-regular");
    }
  });
});

describe("independent checker boundary", () => {
  it("never mounts independent checks in the automatic visible verification", () => {
    const visible = checkArguments("/fixture", undefined, "nonce");
    expect(visible).toContain("--unshare-all");
    expect(visible).toContain("--clearenv");
    expect(visible).not.toContain("/oracle");
    expect(visible).not.toContain("--bind");
    const independent = checkArguments("/fixture", "/hidden", "nonce");
    expect(independent.slice(independent.indexOf("/hidden") - 1, independent.indexOf("/hidden") + 2))
      .toEqual(["--ro-bind", "/hidden", "/oracle"]);
  });

  it("requires an executed-test completion record, not just exit zero or reassuring output", () => {
    const record = { testsRun: 5, failures: 0, errors: 0, skipped: 0 };
    const output = (value: object, token = "nonce") => `POTLUCK_EVAL_CHECK:${token}:${JSON.stringify(value)}\n`;
    expect(checkResult(0, "Ran 5 tests\nOK", "nonce").code).not.toBe(0);
    expect(checkResult(0, output(record, "other-nonce"), "nonce").code).not.toBe(0);
    expect(checkResult(0, output({ ...record, testsRun: 0 }), "nonce").code).not.toBe(0);
    expect(checkResult(0, output({ ...record, skipped: 1 }), "nonce").code).not.toBe(0);
    expect(checkResult(0, output(record), "nonce")).toMatchObject({ code: 0, harnessCompleted: true, completion: record });
    expect(checkResult(124, output(record), "nonce").code).toBe(124);
  });

  it("distinguishes broken fixtures from failed test launch", () => {
    expect(() => assertBrokenBaseline({ code: 1, output: "FAILED (errors=1)\nImportError", harnessCompleted: true }, "fixture")).toThrow();
    expect(() => assertBrokenBaseline({ code: 1, output: "FAIL: boundary\nAssertionError\nFAILED", harnessCompleted: true }, "fixture")).not.toThrow();
  });
});

// This executes only repository-owned fixture/reference code, never generated repairs.
// It validates contracts on developer machines; it is not the Linux sandbox self-test.
async function trustedFixtureCheck(cwd: string, tests: string) {
  const token = "trusted-fixture-validation";
  try {
    const { stdout, stderr } = await exec("python3", ["-I", "-B", "-c", checkerScript(cwd, tests, token)], { timeout: 10000 });
    return checkResult(0, stdout + stderr, token);
  } catch (error: any) {
    if (error.code === "ENOENT") throw error;
    return checkResult(typeof error.code === "number" ? error.code : 124, String(error.stdout ?? "") + String(error.stderr ?? ""), token);
  }
}

describe("frozen fixture contracts (no model inference)", () => {
  for (const [suiteName, caseName] of [["repo-edit-v1", "booking"], ["repo-edit-v1", "retry"], ["repo-edit-v1", "contacts"],
    ["repo-edit-transfer-v1", "settings"], ["repo-edit-transfer-v1", "routing"]]) {
    it(`${suiteName}/${caseName}: broken baseline fails visible and independent assertions; reference passes both`, async () => {
      const suite = join(cli, "eval", suiteName);
      const manifest = JSON.parse(await readFile(join(suite, "manifest.json"), "utf8"));
      const fixture = manifest.cases.find((item: { id: string }) => item.id === caseName);
      const root = join(suite, caseName);
      const cwd = await workspace();
      await cp(join(root, "project"), cwd, { recursive: true });
      const baseline = await hashes(cwd);
      const visibleTests = join(cwd, "tests");
      const independentTests = join(root, "oracle");
      for (const testRoot of [visibleTests, independentTests]) {
        const result = await trustedFixtureCheck(cwd, testRoot);
        expect(() => assertBrokenBaseline(result, caseName), result.output).not.toThrow();
      }
      for (const name of fixture.editable) await cp(join(root, "reference", name), join(cwd, name));
      const visible = await trustedFixtureCheck(cwd, visibleTests);
      const oracle = await trustedFixtureCheck(cwd, independentTests);
      expect(visible.code, visible.output).toBe(0);
      expect(oracle.code, oracle.output).toBe(0);
      expect(gradeTrial({ visible, oracle, events: [] }, baseline, await inspectWorkspace(cwd, fixture.editable), fixture.editable, { selfTest: true }).passed).toBe(true);
      expect(Object.keys(await hashes(cwd)).some(path => path.includes("oracle") || path.includes("reference"))).toBe(false);
    });
  }

  it("rejects early successful process exit during fixture import", async () => {
    const cwd = await workspace();
    await mkdir(join(cwd, "tests"));
    await writeFile(join(cwd, "tests", "test_early_exit.py"), "import os\nos._exit(0)\n");
    const result = await trustedFixtureCheck(cwd, join(cwd, "tests"));
    expect(result.code).not.toBe(0);
    expect(result.harnessCompleted).toBe(false);
  });
});
