import { afterEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { setupMachine } from "../src/operational-setup.js";
import { loadCliPrefs, saveCliPrefs } from "../src/config.js";
import { manageGateway } from "../src/manage.js";
import { connect, household, login, memberStatus } from "../src/member.js";
import { startRuntime } from "../src/runtime.js";
vi.mock("../src/config.js", () => ({ loadCliPrefs: vi.fn(), saveCliPrefs: vi.fn() }));
vi.mock("../src/manage.js", () => ({ manageGateway: vi.fn() }));
vi.mock("../src/member.js", () => ({ connect: vi.fn(), household: vi.fn(), login: vi.fn(), memberStatus: vi.fn() }));
vi.mock("../src/runtime.js", async original => ({ ...await original<typeof import("../src/runtime.js")>(), startRuntime: vi.fn() }));
afterEach(() => vi.resetAllMocks());
function defaults() {
  vi.mocked(loadCliPrefs).mockResolvedValue({});
  vi.mocked(startRuntime).mockResolvedValue({ schemaVersion: 1, state: "running", started: false });
  vi.mocked(memberStatus).mockResolvedValue({ signedIn: false });
}
it("sets up locally without implicitly enabling access or network routing", async () => {
  defaults();
  expect(await setupMachine(parseArgs(["setup", "--json"]))).toMatchObject({ state: "configured", scope: "local" });
  expect(saveCliPrefs).toHaveBeenCalledWith({ scope: "local" });
  expect(manageGateway).not.toHaveBeenCalled(); expect(login).not.toHaveBeenCalled(); expect(connect).not.toHaveBeenCalled();
});
it("preserves preferences and resumes explicit connection without creating another household", async () => {
  defaults();
  vi.mocked(loadCliPrefs).mockResolvedValue({ scope: "mine", model: "custom" });
  vi.mocked(memberStatus).mockResolvedValue({ signedIn: true });
  vi.mocked(household).mockResolvedValue({ household: { id: "existing" } });
  await setupMachine(parseArgs(["setup", "--no-input", "--connect", "--enable-gateway"]));
  expect(saveCliPrefs).not.toHaveBeenCalled(); expect(login).not.toHaveBeenCalled();
  expect(household).toHaveBeenCalledTimes(1); expect(connect).toHaveBeenCalledTimes(1); expect(manageGateway).toHaveBeenCalledTimes(1);
});
it("returns pending approval instructions in unattended setup and can resume later", async () => {
  defaults();
  vi.mocked(login).mockResolvedValue({ state: "pending", userCode: "BCDF-GHJK" });
  expect(await setupMachine(parseArgs(["setup", "--json", "--sign-in", "--connect"]))).toMatchObject({ state: "pending", account: { userCode: "BCDF-GHJK" } });
  expect(connect).not.toHaveBeenCalled();
});
it("EOF cancels setup without consenting to gateway or account changes", async () => {
  defaults();
  await expect(setupMachine(parseArgs(["setup"]), { ask: async () => null, progress: () => {} })).rejects.toThrow("interrupted");
  expect(manageGateway).not.toHaveBeenCalled(); expect(login).not.toHaveBeenCalled();
});
