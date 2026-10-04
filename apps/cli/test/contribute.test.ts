import { afterEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { manageContribution } from "../src/contribute.js";
import { runtimeRequest } from "../src/runtime.js";
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn(async () => ({ schemaVersion: 1, enabled: false, api_key: "never-echo" })) }));
afterEach(() => vi.clearAllMocks());
it("sends only requested limits and never enables contribution as a side effect", async () => {
  const report = await manageContribution(parseArgs(["contribute", "limits", "--jobs", "2", "--max-tokens", "512", "--min-free-memory", "2"]));
  expect(vi.mocked(runtimeRequest).mock.calls[0][1].body).toEqual({ maxJobs: 2, maxTokens: 512, minFreeMemoryGiB: 2 });
  expect(JSON.stringify(report)).not.toContain("never-echo");
});
it("encodes an explicit schedule and can remove it independently", async () => {
  await manageContribution(parseArgs(["contribute", "schedule", "22:00-06:00", "--days", "mon,fri", "--timezone", "America/New_York"]));
  expect(vi.mocked(runtimeRequest).mock.calls[0][1].body).toEqual({ schedule: { start: "22:00", end: "06:00", days: [0, 4], timezone: "America/New_York" } });
  await manageContribution(parseArgs(["contribute", "schedule", "off"]));
  expect(vi.mocked(runtimeRequest).mock.calls[1][1].body).toEqual({ schedule: null });
});
it("rejects invalid schedule and limit options before calling the service", async () => {
  for (const argv of [["contribute", "limits", "--jobs", "65"], ["contribute", "on", "--max-tokens", "3"], ["status", "--jobs", "1"]]) expect(() => parseArgs(argv)).toThrow();
  for (const argv of [["contribute", "schedule", "25:00-06:00"], ["contribute", "schedule", "22:00-06:00", "--days", "sat,sat"]]) await expect(manageContribution(parseArgs(argv))).rejects.toThrow();
  expect(runtimeRequest).not.toHaveBeenCalled();
});
