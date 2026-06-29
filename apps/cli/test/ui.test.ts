import { describe, expect, it } from "vitest";

import { oneShotConfirm } from "../src/ui.js";

describe("oneShotConfirm (-p mode gate)", () => {
  it("refuses gated actions without --yes and explains how to allow them", async () => {
    const lines: string[] = [];
    const confirm = oneShotConfirm(false, (l) => lines.push(l));
    const ok = await confirm({ tool: "write_file", summary: "", subject: "a.txt" });
    expect(ok).toBe(false);
    expect(lines.join("\n")).toMatch(/--yes/);
    expect(lines.join("\n")).toMatch(/refusing write_file/);
  });

  it("refuses run_command without --yes", async () => {
    const lines: string[] = [];
    const confirm = oneShotConfirm(false, (l) => lines.push(l));
    expect(await confirm({ tool: "run_command", summary: "rm -rf x", subject: "rm -rf x" })).toBe(
      false,
    );
  });

  it("auto-approves every gated action with --yes", async () => {
    const lines: string[] = [];
    const confirm = oneShotConfirm(true, (l) => lines.push(l));
    expect(await confirm({ tool: "edit_file", summary: "", subject: "f.ts" })).toBe(true);
    expect(await confirm({ tool: "run_command", summary: "ls", subject: "ls" })).toBe(true);
    expect(lines.join("\n")).toMatch(/auto-approved/);
  });
});
