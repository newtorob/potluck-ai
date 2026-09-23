import { createInterface } from "node:readline/promises";
import type { CliArgs } from "./args.js";
import { UsageError } from "./args.js";
import { loadCliPrefs, saveCliPrefs } from "./config.js";
import { manageGateway } from "./manage.js";
import { connect, household, login, memberStatus } from "./member.js";
import { RuntimeError, startRuntime } from "./runtime.js";

export interface OnboardingIO {
  ask: (question: string) => Promise<string | null>;
  progress: (message: string) => void;
}

/** Resume from service state on every run. An unanswered prompt never opts in. */
export async function setupMachine(args: CliArgs, io?: OnboardingIO): Promise<Record<string, unknown>> {
  const runtime = await startRuntime(args.timeout);
  const prefs = await loadCliPrefs();
  if (!prefs.scope) await saveCliPrefs({ ...prefs, scope: "local" });
  const report: Record<string, unknown> = { schemaVersion: 1, state: "configured", runtime, scope: prefs.scope || "local" };
  const consent = async (question: string): Promise<boolean> => {
    if (!io) return false;
    const answer = await io.ask(question + " [y/N] ");
    if (answer === null) throw new RuntimeError("Setup interrupted. Run potluck setup again to resume.");
    return /^(y|yes)$/i.test(answer.trim());
  };
  if (args.enableGateway || await consent("Enable the authenticated local gateway for coding agents and other tools?")) {
    report.gateway = await manageGateway({ ...args, action: "enable" });
  }
  let member = await memberStatus(args);
  if (!member.signedIn && (args.signIn || await consent("Sign in to your Potluck account using a browser on any device?"))) {
    member = await login({ ...args, wait: Boolean(io) }, io?.progress, Boolean(io));
    report.account = member;
    if (member.state !== "signed_in") return { ...report, state: member.state, next: "Complete potluck login --wait, then rerun setup with the same options." };
  }
  report.account = member;
  const pair = args.connect || (member.signedIn && await consent("Connect this machine to your household?"));
  if (pair) {
    if (!member.signedIn) throw new RuntimeError("Sign in before connecting: potluck login, then rerun setup --connect.");
    let current = await household({ ...args, action: "list" });
    if (!current.household) current = await household({ ...args, action: "create", name: undefined });
    report.household = current;
    report.connection = await connect(args);
  }
  report.next = "Run potluck models list, install/load a model, then potluck doctor. Use potluck setup --preferences to customize coding-agent defaults.";
  return report;
}

export async function runMachineSetup(args: CliArgs): Promise<Record<string, unknown>> {
  if (args.json || args.noInput) return setupMachine(args);
  if (!process.stdin.isTTY) throw new UsageError("Use potluck setup --no-input with explicit --enable-gateway, --sign-in, or --connect options for unattended setup.");
  const rl = createInterface({ input: process.stdin, output: process.stderr });
  try {
    return await setupMachine(args, {
      ask: async question => {
        try { return await rl.question(question); } catch { return null; }
      },
      progress: message => process.stderr.write(message),
    });
  } finally { rl.close(); }
}
