import { type CliArgs, UsageError } from "./args.js";
import { runtimeRequest } from "./runtime.js";
const weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
export async function manageContribution(args: CliArgs): Promise<Record<string, unknown>> {
  const patch: Record<string, unknown> = {};
  if (args.action === "on" || args.action === "off") patch.enabled = args.action === "on";
  if (args.action === "limits") {
    if (args.jobs !== undefined) patch.maxJobs = args.jobs;
    if (args.maxTokens !== undefined) patch.maxTokens = args.maxTokens;
    if (args.minFreeMemory !== undefined) patch.minFreeMemoryGiB = args.minFreeMemory;
  }
  if (args.action === "schedule") {
    if (args.target === "off") {
      if (args.days || args.timezone) throw new UsageError("Schedule off does not take days or a timezone.");
      patch.schedule = null;
    } else {
      const times = /^((?:[01]\d|2[0-3]):[0-5]\d)-((?:[01]\d|2[0-3]):[0-5]\d)$/.exec(args.target || "");
      if (!times || times[1] === times[2]) throw new UsageError("Use different start/end times: contribute schedule 22:00-06:00.");
      const days = args.days ? args.days.toLowerCase().split(",").map(day => weekdays.indexOf(day)) : [0, 1, 2, 3, 4, 5, 6];
      if (days.includes(-1) || new Set(days).size !== days.length) throw new UsageError("Use unique comma-separated weekdays, e.g. --days mon,tue,wed,thu,fri.");
      if (args.timezone) { try { new Intl.DateTimeFormat("en", { timeZone: args.timezone }); } catch { throw new UsageError("Use an IANA timezone, e.g. America/New_York."); } }
      patch.schedule = { start: times[1], end: times[2], days, timezone: args.timezone ?? null };
    }
  }
  const result = await runtimeRequest("/contributor/controls", { timeout: args.timeout,
    ...(Object.keys(patch).length ? { method: "PATCH" as const, body: patch } : {}) });
  // Never print credentials or arbitrary additions to the shared API response.
  return Object.fromEntries(["schemaVersion", "enabled", "schedule", "maxJobs", "maxTokens", "minFreeMemoryGiB",
    "state", "acceptingJobs", "pauseReason", "signedIn", "jobsInFlight", "jobsServed", "lastError"].map(key => [key, result[key]]));
}
export function formatContribution(report: Record<string, unknown>): string {
  const schedule = report.schedule as { start: string; end: string; days: number[]; timezone: string | null } | null;
  return `Coordinator contribution ${report.enabled ? "enabled" : "disabled"}: ${report.state}.\n` +
    `Accepting new jobs: ${report.acceptingJobs ? "yes" : "no"}${report.pauseReason ? ` (${report.pauseReason})` : ""}. In flight: ${report.jobsInFlight}. Served: ${report.jobsServed}.\n` +
    `Limits: ${report.maxJobs} concurrent jobs; ${report.maxTokens ?? "no configured cap on"} output tokens per job; ${report.minFreeMemoryGiB} GiB free memory required at admission.\n` +
    `Schedule: ${schedule ? `${schedule.start}-${schedule.end} on ${schedule.days.map(day => weekdays[day]).join(",")} (${schedule.timezone ?? "machine local time"})` : "all day"}.\n` +
    `${report.signedIn ? "" : "Run potluck login to identify this contributor.\n"}${report.lastError ? `Connection issue: ${report.lastError}.\n` : ""}`;
}
