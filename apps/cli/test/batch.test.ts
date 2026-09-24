import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtemp, writeFile, readFile, mkdir, symlink, stat, rm, link } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "../src/args.js";
import { readBatchFiles, runBatch } from "../src/batch.js";
import { runtimeRequest } from "../src/runtime.js";
import { readProjectModelLock } from "../src/model-lock.js";
import { ControlError } from "../src/control.js";

vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn() }));
vi.mock("../src/model-lock.js", async original => ({ ...await original<typeof import("../src/model-lock.js")>(), readProjectModelLock: vi.fn() }));
let dir: string;
const id = "d3c2ef42-7c2d-45ca-bc78-df35cc4488cc";
const model = { id: "tiny", sha256: "a".repeat(64) };
const report = { schemaVersion: 1, id, state: "completed", reason: null, scope: "mine", model, total: 1,
  retries: 0, counts: { completed: 1, running: 0, pending: 0, failed: 0 },
  items: [{ id: "1", name: "a.txt", inputSha256: "b".repeat(64), state: "completed", error: null, attempts: [] }] };
beforeEach(async () => {
  dir = await mkdtemp(join(tmpdir(), "potluck-batch-test-"));
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: join(dir, "potluck.lock"), model: { ...model, format: "gguf", sizeBytes: 12 } });
});
afterEach(async () => { vi.resetAllMocks(); await rm(dir, { recursive: true, force: true }); });
async function run(args: string[]) { let stdout = ""; const code = await runBatch(parseArgs(args), t => stdout += t); return { code, stdout }; }

describe("batch CLI", () => {
  it.each([
    ["batch", "submit", "a.txt"], ["batch", "submit", "--scope", "mine"],
    ["batch", "submit", "a", "--scope", "pool"], ["batch", "submit", "a", "--scope", "mine", "--jobs", "0"],
    ["batch", "submit", "a", "--scope", "mine", "--jobs", "9"],
    ["batch", "submit", "a", "--scope", "mine", "--max-tokens", "32"],
    ["batch", "submit", "a", "--scope", "mine", "--model", "other"],
    ["batch", "status", "../secret"], ["batch", "list", id], ["batch", "resume"],
    ["batch", "resume", id, "--jobs", "3"], ["batch", "status", id, "--scope", "local"],
    ["batch", "submit", "a", "--scope", "local", "--watch"], ["batch", "status", id, "--output", "x"],
  ])("rejects invalid command %j before reading or sending", (...argv) => {
    expect(() => parseArgs(argv)).toThrow(); expect(runtimeRequest).not.toHaveBeenCalled();
  });

  it("snapshots explicit files and transmits only labels/content and immutable pin", async () => {
    const file = join(dir, "a.txt"); await writeFile(file, "Meeting: launch Friday.");
    vi.mocked(runtimeRequest).mockResolvedValue({ ...report, state: "queued" });
    const result = await run(["batch", "submit", file, "--scope", "mine", "--json"]);
    expect(result.code).toBe(0);
    const [path, options] = vi.mocked(runtimeRequest).mock.calls[0];
    expect(path).toBe("/batches");
    expect(options?.body).toMatchObject({ model, scope: "mine", concurrency: 3, max_tokens: 512,
      files: [{ name: "a.txt", text: "Meeting: launch Friday." }] });
    expect(JSON.stringify(options?.body)).not.toContain(dir);
    expect(result.stdout).not.toContain("Meeting");
  });

  it("requires a project lock and never changes the runtime model", async () => {
    vi.mocked(readProjectModelLock).mockResolvedValue(undefined);
    await expect(run(["batch", "submit", "a", "--scope", "mine"])).rejects.toThrow("require potluck.lock");
    expect(runtimeRequest).not.toHaveBeenCalled();
  });

  it("rejects directories, leaf symlinks, binary/oversize files and duplicate inodes", async () => {
    const folder = join(dir, "folder"); await mkdir(folder);
    const file = join(dir, "a"); await writeFile(file, "text");
    const alias = join(dir, "alias"); await symlink(file, alias);
    const hard = join(dir, "hard"); await link(file, hard);
    const binary = join(dir, "binary"); await writeFile(binary, Buffer.from([0xff, 0x00]));
    const big = join(dir, "big"); await writeFile(big, "x".repeat(16385));
    for (const files of [[folder], [alias], [binary], [big], [file, file], [file, hard]]) await expect(readBatchFiles(files)).rejects.toThrow();
    expect(runtimeRequest).not.toHaveBeenCalled();
  });

  it("enforces the combined byte limit", async () => {
    const files: string[] = [];
    for (let n = 0; n < 17; n++) { const f = join(dir, `${n}.txt`); await writeFile(f, "x".repeat(16384)); files.push(f); }
    await expect(readBatchFiles(files)).rejects.toThrow("256 KiB");
  });

  it("preserves original policy on resume and refuses conflicting pins or scope", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ ...report, state: "paused" });
    await run(["batch", "resume", id]);
    expect(runtimeRequest).toHaveBeenLastCalledWith(`/batches/${id}/resume`, { method: "POST", timeout: 3000, body: { model, scope: "mine" } });
    vi.mocked(runtimeRequest).mockClear();
    await expect(run(["batch", "resume", id, "--scope", "local"])).rejects.toThrow("original project");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
    vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "potluck.lock", model: { ...model, sha256: "c".repeat(64), format: "gguf", sizeBytes: 12 } });
    await expect(run(["batch", "resume", id])).rejects.toThrow("original project");
  });

  it("writes completed results exclusively to a private output file", async () => {
    vi.mocked(runtimeRequest).mockImplementation(async path => path.endsWith("/results/1") ? { id: "1", name: "a.txt", state: "completed", text: "Summary", inputSha256: "b".repeat(64) } : report);
    const output = join(dir, "result.json");
    expect((await run(["batch", "results", id, "--json", "--output", output])).code).toBe(0);
    expect(JSON.parse(await readFile(output, "utf8")).results[0].text).toBe("Summary");
    expect((await stat(output)).mode & 0o777).toBe(0o600);
    await expect(run(["batch", "results", id, "--output", output])).rejects.toThrow();
    expect(JSON.parse(await readFile(output, "utf8")).results[0].text).toBe("Summary");
  });

  it("stops watch on paused state, returns nonzero and does not cancel the job", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ ...report, state: "paused", reason: "no_eligible_workers" });
    const result = await run(["batch", "status", id, "--watch", "--json"]);
    expect(result.code).toBe(1); expect(JSON.parse(result.stdout).state).toBe("paused");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });

  it("reports an uncertain submission ID without resubmitting", async () => {
    const file = join(dir, "a.txt"); await writeFile(file, "text");
    vi.mocked(runtimeRequest).mockRejectedValue(new ControlError("timeout", "Request timed out"));
    await expect(run(["batch", "submit", file, "--scope", "local"])).rejects.toThrow(/Check potluck batch status [a-f0-9-]+ before submitting again/);
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
});
