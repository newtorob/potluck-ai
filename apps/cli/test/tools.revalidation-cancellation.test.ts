import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { runTool } from "../src/tools.js";

const revalidation = vi.hoisted(() => ({ afterRead: undefined as (() => void) | undefined }));
vi.mock("node:fs/promises", async importOriginal => {
  const fs = await importOriginal<typeof import("node:fs/promises")>();
  return { ...fs, readFile: async (...args: Parameters<typeof fs.readFile>) => {
    const result = await fs.readFile(...args);
    revalidation.afterRead?.();
    return result;
  } };
});

let cwd: string;
beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "potluck-revalidation-cancel-"));
  await writeFile(join(cwd, "code.txt"), "original\n");
});
afterEach(async () => {
  revalidation.afterRead = undefined;
  await rm(cwd, { recursive: true, force: true });
});

it.each(["edit_file", "write_file"] as const)("does not %s after cancellation during approved-file revalidation", async tool => {
  const controller = new AbortController();
  const cancelledDuringRead = vi.fn(() => {
    revalidation.afterRead = undefined;
    controller.abort();
  });
  const confirm = vi.fn(async () => {
    expect(controller.signal.aborted).toBe(false);
    // The initial read already completed. Abort only when the asynchronous
    // revalidation read returns, after the post-approval cancellation check.
    revalidation.afterRead = cancelledDuringRead;
    return true;
  });
  const args = tool === "edit_file"
    ? { path: "code.txt", old_string: "original", new_string: "changed" }
    : { path: "code.txt", content: "changed\n" };
  await expect(runTool(tool, args, { cwd, confirm, signal: controller.signal }))
    .rejects.toMatchObject({ name: "AbortError" });
  expect(confirm).toHaveBeenCalledOnce();
  expect(cancelledDuringRead).toHaveBeenCalledOnce();
  expect(await readFile(join(cwd, "code.txt"), "utf8")).toBe("original\n");
});
