import { afterEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { formatCircles, manageCircles } from "../src/circle.js";
import { runtimeRequest } from "../src/runtime.js";

const preview = { circle_name: "Friends", invited_by: "Ada", people: 2, already_in: false, token: "never-echo" };
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn(async (path: string, options: { method?: string }) => {
  if (path === "/circles" && !options.method) return { schemaVersion: 1, circles: [
    { id: "c1", name: "Friends", role: "owner", people: [{ name: "Ada", role: "owner", is_you: true }, { name: "Grace", role: "member", is_you: false }],
      models: [{ slug: "qwen3-4b-q4", machines: 2 }] },
    { id: "c2", name: "Work", role: "member", people: [], models: [] }] };
  if (path === "/circles") return { id: "c3", name: "Lab", role: "owner", secret: "never-echo" };
  if (path.endsWith("/invites")) return { code: "ABCD-EFGH-JKMN", url: "https://trypotluck.ai/circles/join?code=ABCD-EFGH-JKMN", expires_at: "2026-10-15T00:00:00Z", max_uses: 1 };
  if (path === "/circles/invites/preview") return preview;
  if (path === "/circles/invites/accept") return { id: "c1", name: "Friends", role: "member" };
  return {};
}) }));
const calls = () => vi.mocked(runtimeRequest).mock.calls.map(([path, options]) => [options.method ?? "GET", path, options.body]);
afterEach(() => { vi.clearAllMocks(); preview.already_in = false; });

it("lists circles with who is in them and what each offers now", async () => {
  const text = formatCircles(await manageCircles(parseArgs(["circle"])));
  expect(text).toContain("Friends: you own it");
  expect(text).toContain("People: Ada (you), owner; Grace");
  expect(text).toContain("Offering now: qwen3-4b-q4 (2 machines)");
  expect(text).toContain("Work: you are a member");
  expect(text).toContain("no other member's machine is sharing a model");
});

it("creates a circle and makes an invite for a circle named by the person", async () => {
  const created = await manageCircles(parseArgs(["circle", "create", "Lab"]));
  expect(JSON.stringify(created)).not.toContain("never-echo");
  const invite = formatCircles(await manageCircles(parseArgs(["circle", "invite", "My Friends"])));
  expect(calls()).toEqual([["POST", "/circles", { name: "Lab" }], ["POST", "/circles/My%20Friends/invites", {}]]);
  expect(invite).toContain("Invite code: ABCD-EFGH-JKMN");
  expect(invite).toContain("works once");
  expect(invite).toContain("potluck circle join ABCD-EFGH-JKMN");
});

it("shows what an invite is for and joins only with --yes", async () => {
  const looked = await manageCircles(parseArgs(["circle", "join", "abcd-efgh-jkmn"]));
  expect(looked.joined).toBe(false);
  expect(calls()).toEqual([["POST", "/circles/invites/preview", { code: "abcd-efgh-jkmn" }]]);
  const text = formatCircles(looked);
  expect(text).toContain("Friends: invited by Ada, 2 people in it.");
  expect(text).toContain("The machine that answers sees the conversation.");
  expect(text).toContain("--yes");
  expect(JSON.stringify(looked)).not.toContain("never-echo");

  const joined = await manageCircles(parseArgs(["circle", "join", "abcd-efgh-jkmn", "--yes"]));
  expect(joined.joined).toBe(true);
  expect(calls().at(-1)).toEqual(["POST", "/circles/invites/accept", { code: "abcd-efgh-jkmn" }]);
  expect(formatCircles(joined)).toContain("potluck contribute share circles");
});

it("does not join again when already in the circle", async () => {
  preview.already_in = true;
  const report = await manageCircles(parseArgs(["circle", "join", "ABCD-EFGH-JKMN", "--yes"]));
  expect(report.joined).toBe(false);
  expect(calls()).toHaveLength(1);
  expect(formatCircles(report)).toContain("already in this circle");
});

it("leaves or deletes only with --yes, and says what that does", async () => {
  await expect(manageCircles(parseArgs(["circle", "leave", "Friends"]))).rejects.toThrow("stops the circle using your machines");
  await expect(manageCircles(parseArgs(["circle", "delete", "Friends"]))).rejects.toThrow("ends the circle for everyone");
  expect(runtimeRequest).not.toHaveBeenCalled();
  await manageCircles(parseArgs(["circle", "leave", "Friends", "--yes"]));
  await manageCircles(parseArgs(["circle", "delete", "Friends", "--yes"]));
  expect(calls().map(c => c.slice(0, 2))).toEqual([["DELETE", "/circles/Friends/membership"], ["DELETE", "/circles/Friends"]]);
});

it("explains engine error codes and rejects malformed commands before calling anything", async () => {
  await manageCircles(parseArgs(["circle", "list"]));
  const hints = vi.mocked(runtimeRequest).mock.calls[0][1].errorHints!;
  expect(hints.invite_invalid).toContain("Ask for a new one");
  expect(hints.signed_out).toContain("potluck login");
  for (const argv of [["circle", "create"], ["circle", "join"], ["circle", "list", "x"], ["circle", "nope"], ["circle", "create", "a", "b"],
    ["circle", "list", "--yes"], ["status", "--yes"]]) expect(() => parseArgs(argv)).toThrow();
});
