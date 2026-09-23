/** Account and household operations. Member/device bearers stay in the engine. */
import { constants } from "node:fs";
import { open, unlink } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import type { CliArgs } from "./args.js";
import { ControlError, meshSocketPath, requestJSON } from "./control.js";
import { plainText } from "./logs.js";
import { RuntimeError, runtimeRequest } from "./runtime.js";

type Report = Record<string, unknown>;
const text = (v: unknown): string | null => typeof v === "string" ? plainText(v) : null;

async function account(path: string, args: CliArgs, method: "GET" | "POST" = "GET", body?: unknown): Promise<Report> {
  try { return await runtimeRequest(`/account${path}`, { method, body, timeout: args.timeout }); }
  catch (error) {
    if (error instanceof ControlError) {
      if (error.status === 401) throw new RuntimeError("Member sign-in is required. Run potluck login.");
      if (error.status === 409 && path === "/household/invite") throw new RuntimeError("Create your household first: potluck household create.");
      if (error.status === 503 && path.startsWith("/login")) throw new RuntimeError("Device sign-in is unavailable on this server. Update the account server and retry.");
    }
    throw error;
  }
}

function publicAccount(result: Report): Report {
  return { signedIn: result.signedIn === true, memberId: text(result.memberId), email: text(result.email), expiresAt: text(result.expiresAt) };
}

export async function login(args: CliArgs, progress: (message: string) => void = () => {}, interactive = Boolean(process.stdin.isTTY)): Promise<Report> {
  let result = await account("/login/start", args, "POST");
  if (result.state === "pending") {
    const instruction = `Open ${text(result.verificationUri)} on any device. Enter code ${text(result.userCode)}, then approve the email link.\n`;
    const wait = args.wait ?? (interactive && !args.json);
    if (!wait) return { schemaVersion: 1, state: "pending", userCode: text(result.userCode), verificationUri: text(result.verificationUri),
      expiresAt: result.expiresAt, interval: result.interval, next: "After approval, run potluck login --wait to finish signing in." };
    progress(instruction);
    // Each HTTP call is bounded; the overall wait is bounded by the grant's expiry.
    const deadline = Math.min(Number(result.expiresAt) * 1000, Date.now() + 1800000);
    if (!Number.isFinite(deadline)) throw new RuntimeError("Invalid sign-in expiry. Restart login.");
    while (result.state === "pending" && Date.now() < deadline) {
      result = await account("/login/poll", args, "POST");
      if (result.state === "pending") await delay(Math.min(60, Math.max(5, Number(result.interval) || 5)) * 1000);
    }
    if (result.state === "pending") result = { state: "expired" };
  }
  return { schemaVersion: 1, state: text(result.state), ...publicAccount(result) };
}

export async function memberStatus(args: CliArgs): Promise<Report> {
  return { schemaVersion: 1, ...publicAccount(await account("", args)) };
}

export async function logout(args: CliArgs): Promise<Report> {
  try {
    const result = await account("/logout", args, "POST", { local_only: args.local === true });
    return { schemaVersion: 1, signedIn: false, revoked: result.revoked === true };
  } catch (error) {
    if (error instanceof ControlError && error.status === 502) throw new RuntimeError("Could not revoke the session online. Retry, or use potluck logout --local to remove it from this machine only.");
    throw error;
  }
}

function householdReport(result: Report): Report {
  const h = (result.household ?? (typeof result.id === "string" ? result : null)) as Report | null;
  const machines = Array.isArray(result.machines) ? result.machines as Report[] : [];
  return { schemaVersion: 1, household: h ? { id: text(h.id), name: text(h.name), meshCidr: text(h.mesh_cidr) } : null,
    machines: machines.map(m => ({ id: text(m.node_id), name: text(m.node_name), platform: text(m.platform), meshIp: text(m.mesh_ip), lastSeenAt: text(m.last_seen_at) })) };
}

interface Invite { schemaVersion: 1; token: string; householdId: string; coordinatorUrl: string; expiresAt: string }
function validOrigin(raw: unknown): string {
  try {
    if (typeof raw !== "string") throw new Error();
    const u = new URL(raw);
    if (u.username || u.password || u.search || u.hash || u.pathname !== "/" ||
      !(u.protocol === "https:" || (u.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname)))) throw new Error();
    return u.origin;
  } catch { throw new RuntimeError("Invalid coordinator origin. Use HTTPS, or HTTP on loopback for development."); }
}

function validateInvite(raw: Report): Invite {
  if (raw.schemaVersion !== 1 || typeof raw.token !== "string" || raw.token.length < 16 || raw.token.length > 4096
    || typeof raw.householdId !== "string" || !raw.householdId || typeof raw.expiresAt !== "string"
    || !Number.isFinite(Date.parse(raw.expiresAt))) throw new RuntimeError("Invalid invitation file.");
  if (Date.parse(raw.expiresAt) <= Date.now()) throw new RuntimeError("Invitation expired. Create a new household invitation.");
  return { schemaVersion: 1, token: raw.token, householdId: raw.householdId, expiresAt: raw.expiresAt, coordinatorUrl: validOrigin(raw.coordinatorUrl) };
}

async function newInvite(args: CliArgs): Promise<Invite> {
  const raw = await account("/household/invite", args, "POST");
  return validateInvite({ schemaVersion: 1, token: raw.token, householdId: raw.household_id, expiresAt: raw.expires_at, coordinatorUrl: raw.coordinatorUrl });
}

export async function readInvite(file: string): Promise<Invite> {
  const handle = await open(resolve(file), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) || info.size > 16384) throw new RuntimeError("Invitation must be a private regular file owned by you (chmod 600).");
    try { return validateInvite(JSON.parse(await handle.readFile("utf8"))); }
    catch (error) { if (error instanceof RuntimeError) throw error; throw new RuntimeError("Invalid invitation file."); }
  } finally { await handle.close(); }
}

export async function household(args: CliArgs): Promise<Report> {
  if (args.action !== "invite") return householdReport(await account("/household", args,
    args.action === "create" ? "POST" : "GET", args.action === "create" ? { name: args.name || "My machines" } : undefined));
  // Reserve a new private file before minting a one-time invitation. Never overwrite.
  const path = resolve(args.output!);
  const handle = await open(path, "wx", 0o600);
  try {
    const invite = await newInvite(args);
    await handle.writeFile(JSON.stringify(invite) + "\n");
    await handle.sync();
    return { schemaVersion: 1, state: "invite_saved", path, householdId: invite.householdId, expiresAt: invite.expiresAt };
  } catch (error) { await unlink(path).catch(() => {}); throw error; }
  finally { await handle.close(); }
}

async function mesh(path: string, args: CliArgs, method: "GET" | "POST" | "PATCH" = "GET", body?: unknown): Promise<Report> {
  try { return await requestJSON({ socketPath: meshSocketPath(), path, method, body, timeout: args.timeout, allowEmpty: true }); }
  catch (error) {
    if (error instanceof ControlError && error.code === "unreachable") throw new RuntimeError("Mesh daemon is unavailable. Install/start potluck-mesh, then retry. Run potluck doctor --scope mine for details.");
    if (error instanceof ControlError && error.status === 409) throw new RuntimeError("Pairing is already in progress or this machine is connected. Check potluck status before retrying.");
    throw error;
  }
}

export async function connect(args: CliArgs): Promise<Report> {
  const state = await mesh("/status", args);
  const inviteFile = args.inviteFile ? await readInvite(args.inviteFile) : null;
  if (state.household_id) {
    const desired = inviteFile?.householdId ?? ((await household({ ...args, action: "list" })).household as Report | null)?.id;
    if (state.household_id !== desired) throw new RuntimeError("This machine belongs to a different household. Run potluck disconnect before connecting it again.");
    return { schemaVersion: 1, state: "already_connected", householdId: text(state.household_id), meshIp: text(state.mesh_ip) };
  }
  const invite = inviteFile || await newInvite(args);
  if (validOrigin(state.coordinator_url) !== invite.coordinatorUrl) throw new RuntimeError("The invitation and mesh daemon use different coordinators. Configure the daemon for the invitation's coordinator before retrying.");
  const result = await mesh("/pair", args, "POST", { pairing_token: invite.token, ...(args.name ? { node_name: args.name } : {}) });
  return { schemaVersion: 1, state: "connected", householdId: text(result.household_id), nodeId: text(result.node_id), meshIp: text(result.mesh_ip) };
}

export async function disconnect(args: CliArgs): Promise<Report> {
  await mesh("/unpair", args, "POST");
  return { schemaVersion: 1, state: "disconnected", note: "This machine's local mesh identity was removed. The coordinator may show its old entry until cleanup." };
}

export function formatMember(report: Report): string {
  if (report.state === "pending") return `Open ${report.verificationUri} on any device.\nCode: ${report.userCode}\n${report.next}`;
  if (report.state === "denied" || report.state === "expired") return `Sign-in ${report.state}. Run potluck login to start again.`;
  if (report.signedIn === true) return `Signed in as ${report.email}.`;
  if (report.signedIn === false) return `Signed out.${report.revoked === false ? " No server session was revoked." : ""}`;
  if (report.state === "invite_saved") return `Invitation saved to ${report.path} (expires ${report.expiresAt}).\nTransfer this private file to the other machine, then run potluck connect --invite-file <file>.`;
  if ("household" in report) {
    const h = report.household as Report | null;
    return h ? `${h.name}\n${(report.machines as Report[]).map(m => `${m.name}\t${m.platform}\t${m.meshIp}`).join("\n") || "No machines connected. Run potluck connect."}` : "No household yet. Run potluck household create.";
  }
  return `Machine ${String(report.state).replaceAll("_", " ")}.${report.meshIp ? ` Mesh IP: ${report.meshIp}.` : ""}${report.note ? `\n${report.note}` : ""}`;
}
