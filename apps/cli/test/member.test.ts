import { chmod, mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { approvalLink, canOpenBrowser, connect, disconnect, formatMember, household, login, logout, memberStatus, readInvite } from "../src/member.js";
import { ControlError, requestJSON } from "../src/control.js";
import { runtimeRequest } from "../src/runtime.js";

vi.mock("../src/runtime.js", async original => ({ ...await original<typeof import("../src/runtime.js")>(), runtimeRequest: vi.fn() }));
vi.mock("../src/control.js", async original => ({ ...await original<typeof import("../src/control.js")>(), requestJSON: vi.fn() }));
let dir: string;
const raw = () => ({ token: "private-invite-credential", household_id: "household-id", coordinatorUrl: "https://coordinator.example",
  expires_at: new Date(Date.now() + 600000).toISOString() });
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "potluck-member-")); });
afterEach(async () => { vi.resetAllMocks(); await rm(dir, { recursive: true, force: true }); });

describe("member commands", () => {
  it("returns an SSH approval code without waiting or exposing bearers", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://example/auth/device", expiresAt: Date.now() / 1000 + 900,
      interval: 5, deviceCode: "private-device", token: "private-session" });
    const result = await login(parseArgs(["login", "--json"]), undefined, false);
    expect(result.state).toBe("pending");
    expect(result.userCode).toBe("BCDF-GHJK");
    expect(JSON.stringify(result)).not.toContain("private");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
  it("resumes approval and exposes only the member profile", async () => {
    vi.mocked(runtimeRequest).mockResolvedValueOnce({ state: "pending", expiresAt: Date.now() / 1000 + 900 })
      .mockResolvedValueOnce({ state: "signed_in", signedIn: true, email: "m@example.com", token: "private" });
    const result = await login(parseArgs(["login", "--wait"]));
    expect(result).toMatchObject({ state: "signed_in", signedIn: true, email: "m@example.com" });
    expect(JSON.stringify(result)).not.toContain("private");
  });
  it("opens the browser with the code filled in, then waits", async () => {
    vi.mocked(runtimeRequest).mockResolvedValueOnce({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device",
      expiresAt: Date.now() / 1000 + 900, interval: 5 })
      .mockResolvedValueOnce({ state: "signed_in", signedIn: true, email: "m@example.com" });
    const opened: string[] = [];
    const messages: string[] = [];
    const result = await login(parseArgs(["login"]), m => messages.push(m), true, url => { opened.push(url); return true; });
    expect(opened).toEqual(["https://trypotluck.ai/auth/device?code=BCDF-GHJK"]);
    expect(messages.join("")).toContain("Opening your browser");
    expect(messages.join("")).toContain("BCDF-GHJK");
    expect(result).toMatchObject({ state: "signed_in", signedIn: true });
  });
  it("prints a link with the code when no browser can open", async () => {
    vi.mocked(runtimeRequest).mockResolvedValueOnce({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device",
      expiresAt: Date.now() / 1000 + 900, interval: 5 })
      .mockResolvedValueOnce({ state: "signed_in", signedIn: true, email: "m@example.com" });
    const messages: string[] = [];
    await login(parseArgs(["login"]), m => messages.push(m), true, null);
    expect(messages.join("")).toContain("open https://trypotluck.ai/auth/device?code=BCDF-GHJK on any device");
    expect(messages.join("")).not.toContain("Opening your browser");
  });
  it("falls back to the printed link when the browser fails to launch", async () => {
    vi.mocked(runtimeRequest).mockResolvedValueOnce({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device",
      expiresAt: Date.now() / 1000 + 900, interval: 5 })
      .mockResolvedValueOnce({ state: "signed_in", signedIn: true });
    const messages: string[] = [];
    await login(parseArgs(["login"]), m => messages.push(m), true, () => false);
    expect(messages.join("")).toContain("open https://trypotluck.ai/auth/device?code=BCDF-GHJK on any device");
  });
  it("never opens a browser when returning the pending code", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device",
      expiresAt: Date.now() / 1000 + 900, interval: 5 });
    const opener = vi.fn(() => true);
    const result = await login(parseArgs(["login", "--no-wait"]), undefined, true, opener);
    expect(opener).not.toHaveBeenCalled();
    expect(result).toMatchObject({ state: "pending", verificationUriComplete: "https://trypotluck.ai/auth/device?code=BCDF-GHJK" });
  });
  it("shows the link with the code for a pending login", () => {
    const text = formatMember({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device",
      verificationUriComplete: "https://trypotluck.ai/auth/device?code=BCDF-GHJK", next: "Then wait." });
    expect(text).toContain("Open https://trypotluck.ai/auth/device?code=BCDF-GHJK on any device.");
    expect(formatMember({ state: "pending", userCode: "BCDF-GHJK", verificationUri: "https://trypotluck.ai/auth/device", next: "x" }))
      .toContain("Open https://trypotluck.ai/auth/device on any device.");
  });
  it("decides when a browser can open", () => {
    const login_ = parseArgs(["login"]);
    expect(canOpenBrowser(login_, {}, "darwin")).toBe(true);
    expect(canOpenBrowser(login_, { DISPLAY: ":0" }, "linux")).toBe(true);
    expect(canOpenBrowser(login_, { WAYLAND_DISPLAY: "wayland-0" }, "linux")).toBe(true);
    expect(canOpenBrowser(login_, {}, "linux")).toBe(false);
    expect(canOpenBrowser(login_, { SSH_CONNECTION: "1.2.3.4 5 6.7.8.9 22" }, "darwin")).toBe(false);
    expect(canOpenBrowser(login_, { SSH_TTY: "/dev/ttys001", DISPLAY: ":0" }, "linux")).toBe(false);
    expect(canOpenBrowser(login_, { CI: "true" }, "darwin")).toBe(false);
    expect(canOpenBrowser(parseArgs(["login", "--no-browser"]), {}, "darwin")).toBe(false);
    expect(canOpenBrowser(parseArgs(["login", "--json"]), {}, "darwin")).toBe(false);
    expect(canOpenBrowser(login_, {}, "win32")).toBe(false);
  });
  it("only builds approval links for secure query-free pages", () => {
    expect(approvalLink("https://trypotluck.ai/auth/device", "BCDF-GHJK")).toBe("https://trypotluck.ai/auth/device?code=BCDF-GHJK");
    expect(approvalLink("http://127.0.0.1:5173/auth/device", "BCDF-GHJK")).toBe("http://127.0.0.1:5173/auth/device?code=BCDF-GHJK");
    expect(approvalLink("http://evil.example/auth/device", "BCDF-GHJK")).toBeNull();
    expect(approvalLink("https://trypotluck.ai/auth/device?x=1", "BCDF-GHJK")).toBeNull();
    expect(approvalLink("not a url", "BCDF-GHJK")).toBeNull();
    expect(approvalLink(null, "BCDF-GHJK")).toBeNull();
  });
  it("shows stored profile without secrets", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ signedIn: true, token: "private", email: "m@example.com" });
    expect(JSON.stringify(await memberStatus(parseArgs(["whoami"])))).not.toContain("private");
  });
  it("offers explicit local logout when server revocation fails", async () => {
    vi.mocked(runtimeRequest).mockRejectedValue(new ControlError("http_error", "HTTP 502", 502));
    await expect(logout(parseArgs(["logout"]))).rejects.toThrow("logout --local");
    vi.mocked(runtimeRequest).mockResolvedValue({ revoked: false });
    expect(await logout(parseArgs(["logout", "--local"]))).toMatchObject({ signedIn: false, revoked: false });
    expect(runtimeRequest).toHaveBeenLastCalledWith("/account/logout", expect.objectContaining({ body: { local_only: true } }));
  });
  it("exports private invitation files and never overwrites existing files", async () => {
    const file = join(dir, "invite.json");
    vi.mocked(runtimeRequest).mockResolvedValue(raw());
    const args = parseArgs(["household", "invite", "--output", file]);
    const result = await household(args);
    expect(JSON.stringify(result)).not.toContain("private-invite");
    expect((await stat(file)).mode & 0o777).toBe(0o600);
    expect((await readInvite(file)).token).toBe(raw().token);
    await expect(household(args)).rejects.toThrow();
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
  it("refuses insecure and symlinked invitations", async () => {
    const file = join(dir, "invite.json");
    await writeFile(file, "{}", { mode: 0o644 });
    await expect(readInvite(file)).rejects.toThrow("private regular");
    await chmod(file, 0o600);
    await symlink(file, join(dir, "link"));
    await expect(readInvite(join(dir, "link"))).rejects.toThrow();
  });
  it("pairs with a private invitation without exposing enrollment secrets", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue(raw());
    const file = join(dir, "invite.json");
    await household(parseArgs(["household", "invite", "--output", file]));
    vi.mocked(runtimeRequest).mockClear();
    vi.mocked(requestJSON).mockResolvedValueOnce({ household_id: null, coordinator_url: raw().coordinatorUrl })
      .mockResolvedValueOnce({ household_id: "household-id", node_id: "node", mesh_ip: "10.0.0.2", node_token: "private-node" });
    const result = await connect(parseArgs(["connect", "--invite-file", file, "--name", "Server"]));
    expect(result).toMatchObject({ state: "connected", nodeId: "node" });
    expect(JSON.stringify(result)).not.toContain("private");
    expect(requestJSON).toHaveBeenLastCalledWith(expect.objectContaining({ path: "/pair", body: { pairing_token: raw().token, node_name: "Server" } }));
    expect(runtimeRequest).not.toHaveBeenCalled(); // Invitation needs no account on the destination.
  });
  it("does not send a pairing token to a different coordinator", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue(raw());
    vi.mocked(requestJSON).mockResolvedValueOnce({ coordinator_url: "https://other.example" });
    await expect(connect(parseArgs(["connect"]))).rejects.toThrow("different coordinators");
    expect(requestJSON).toHaveBeenCalledTimes(1);
  });
  it("does not unpair an existing connection implicitly", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ household: { id: "desired" } });
    vi.mocked(requestJSON).mockResolvedValue({ household_id: "other" });
    await expect(connect(parseArgs(["connect"]))).rejects.toThrow("potluck disconnect");
    expect(requestJSON).toHaveBeenCalledTimes(1);
  });
  it("unpairs explicitly and reports the coordinator cleanup limitation", async () => {
    vi.mocked(requestJSON).mockResolvedValue({});
    const report = await disconnect(parseArgs(["disconnect"]));
    expect(report.state).toBe("disconnected");
    expect(report.note).toContain("cleanup");
    expect(requestJSON).toHaveBeenCalledWith(expect.objectContaining({ path: "/unpair", method: "POST", allowEmpty: true }));
  });
});
