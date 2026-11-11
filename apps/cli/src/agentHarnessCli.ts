#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { PythonCoverageAdapter } from "./agentHarnessDomain.js";
import { HarnessError, prepareHarness, runHarness, harnessStatus, recoverHarness } from "./agentHarness.js";

const [command, first, second, ...extra] = process.argv.slice(2);
const controller = new AbortController();
const interrupt = () => controller.abort();
process.on("SIGINT", interrupt); process.on("SIGTERM", interrupt);
try {
  if (!first || extra.length || (command !== "recover" && !second) || (command === "recover" && second)) {
    throw new HarnessError("Usage: prepare PLAN HUB | run HUB BATCH | status HUB BATCH | recover HUB");
  }
  const python = process.env.POTLUCK_AGENT_DOMAIN_PYTHON, pythonRoot = process.env.POTLUCK_AGENT_DOMAIN_ROOT;
  if (!!python !== !!pythonRoot) throw new HarnessError("Set both domain Python and package root explicitly.");
  const domainAdapter = python && pythonRoot ? new PythonCoverageAdapter(python, pythonRoot) : undefined;
  let result: unknown;
  if (command === "prepare") result = { batchDirectory: await prepareHarness(JSON.parse(await readFile(first, "utf8")), second!, { domainAdapter }) };
  else if (command === "status") result = await harnessStatus(first, second!);
  else if (command === "recover") { await recoverHarness(first); result = { recovered: true }; }
  else if (command === "run") {
    result = await runHarness(first, second!, { endpoint: process.env.POTLUCK_AGENT_URL ?? "", apiKey: process.env.POTLUCK_AGENT_KEY ?? "", signal: controller.signal, domainAdapter });
    const states = (result as Awaited<ReturnType<typeof harnessStatus>>).jobs;
    process.exitCode = controller.signal.aborted ? 130 : states.every(s => s.status === "needs_review") ? 0 : 1;
  } else throw new HarnessError("Unknown command. Use prepare, run, status or recover.");
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
} catch (error) {
  // Runtime/source details and credentials belong only in private local receipts.
  const safe = error instanceof HarnessError ? error.message : "Harness stopped. Check plan, freshness, ownership and saved job state.";
  process.stderr.write(safe + "\n"); process.exitCode = controller.signal.aborted ? 130 : 1;
} finally { process.off("SIGINT", interrupt); process.off("SIGTERM", interrupt); }
