import { type CliArgs, UsageError } from "./args.js";
import { runtimeRequest } from "./runtime.js";

type Report = Record<string, unknown>;
interface Person { name: string; role: string; is_you: boolean }
interface Circle { id: string; name: string; role: string; people?: Person[]; models?: Array<{ slug: string; machines: number }> }

// The engine names what went wrong with a short code; these are the words
// for it. A response body is never printed.
const errorHints: Readonly<Record<string, string>> = {
  signed_out: "Sign in to use trusted circles: potluck login.",
  sign_in_expired: "Your sign-in has expired. Run potluck login.",
  coordinator_unreachable: "Could not reach the coordinator. Retry when connected.",
  invite_invalid: "That invite is not valid. It may have expired or already been used. Ask for a new one.",
  circle_full: "This circle is full.",
  owner_cannot_leave: "You own this circle. Use potluck circle delete to end it for everyone.",
  circle_not_found: "No circle by that name. See yours with potluck circle list.",
  circle_ambiguous: "More than one circle matches. Use its ID from potluck circle list --json.",
  no_circles: "You are not in a circle yet. Create one, or join with an invite code.",
  circle_refused: "The coordinator refused that request.",
};

const brief = (c: Circle): Report => ({ id: c.id, name: c.name, role: c.role });
const path = (circle: string, rest = "") => `/circles/${encodeURIComponent(circle)}${rest}`;

export async function manageCircles(args: CliArgs): Promise<Report> {
  const base = { timeout: args.timeout, errorHints };
  const action = args.action!, target = args.target!;
  if (action === "list") {
    const listed = await runtimeRequest<{ circles: Circle[] }>("/circles", base);
    return { schemaVersion: 1, action, circles: listed.circles };
  }
  if (action === "create") {
    return { schemaVersion: 1, action, circle: brief(await runtimeRequest<Circle>("/circles", { ...base, method: "POST", body: { name: target } })) };
  }
  if (action === "invite") {
    const invite = await runtimeRequest<Report>(path(target, "/invites"), { ...base, method: "POST", body: {} });
    return { schemaVersion: 1, action, circle: target, code: invite.code, url: invite.url, expiresAt: invite.expires_at, maxUses: invite.max_uses };
  }
  if (action === "join") {
    const preview = await runtimeRequest<Report>("/circles/invites/preview", { ...base, method: "POST", body: { code: target } });
    const summary = { circleName: preview.circle_name, invitedBy: preview.invited_by, people: preview.people, alreadyIn: preview.already_in };
    // Joining lets other people use this member's machines, so it is never
    // a side effect of looking at an invite.
    if (!args.yes || preview.already_in) return { schemaVersion: 1, action, joined: false, invite: summary };
    const circle = await runtimeRequest<Circle>("/circles/invites/accept", { ...base, method: "POST", body: { code: target } });
    return { schemaVersion: 1, action, joined: true, invite: summary, circle: brief(circle) };
  }
  if (!args.yes) {
    throw new UsageError(action === "leave"
      ? "Leaving stops the circle using your machines and you using theirs. Run again with --yes."
      : "Deleting ends the circle for everyone in it. Run again with --yes.");
  }
  await runtimeRequest(path(target, action === "leave" ? "/membership" : ""), { ...base, method: "DELETE", allowEmpty: true });
  return { schemaVersion: 1, action, circle: target, done: true };
}

const CONSENT = "Joining lets the people in this circle send requests to your machines that share with trusted circles, and lets you send requests to theirs. The machine that answers sees the conversation. You can leave at any time.";

export function formatCircles(report: Report): string {
  const action = report.action;
  if (action === "list") {
    const circles = report.circles as Circle[];
    if (!circles.length) return "You are not in a circle yet. Create one with potluck circle create <name>, or join with potluck circle join <code>.\n";
    return circles.map(c => {
      const people = (c.people ?? []).map(p => `${p.name}${p.is_you ? " (you)" : ""}${p.role === "owner" ? ", owner" : ""}`).join("; ");
      const models = (c.models ?? []).map(m => m.machines > 1 ? `${m.slug} (${m.machines} machines)` : m.slug).join(", ");
      return `${c.name}: ${c.role === "owner" ? "you own it" : "you are a member"}\n` +
        `  People: ${people || "only you"}\n` +
        `  Offering now: ${models || "nothing; no other member's machine is sharing a model"}\n`;
    }).join("");
  }
  if (action === "create") {
    const c = report.circle as Circle;
    return `Created ${c.name}. Invite someone with potluck circle invite "${c.name}".\n`;
  }
  if (action === "invite") {
    return `Invite code: ${report.code}\nLink: ${report.url}\n` +
      `It works ${report.maxUses === 1 ? "once" : `${report.maxUses} times`}, until ${String(report.expiresAt).slice(0, 10)}, and is shown only now. Send it to one person you trust.\n` +
      `They join with: potluck circle join ${report.code}\n`;
  }
  if (action === "join") {
    const invite = report.invite as Report;
    const head = `${invite.circleName}: invited by ${invite.invitedBy}, ${invite.people} ${invite.people === 1 ? "person" : "people"} in it.\n`;
    if (report.joined) return `${head}You joined. To lend this machine, run potluck contribute share circles and potluck contribute on.\n`;
    if (invite.alreadyIn) return `${head}You are already in this circle.\n`;
    return `${head}${CONSENT}\nTo join, run the same command with --yes.\n`;
  }
  return action === "leave" ? `You left ${report.circle}.\n` : `Deleted ${report.circle}.\n`;
}
