import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// The approval page talks to the account API from the browser. A CSP that
// leaves it out of connect-src blocks every call before it reaches the network.
describe("site CSP", () => {
  it("lets the browser reach the account API", () => {
    const config = JSON.parse(readFileSync(new URL("../../../../vercel.json", import.meta.url), "utf8"));
    const policies: string[] = config.headers
      .flatMap((rule: { headers: { key: string; value: string }[] }) => rule.headers)
      .filter((header: { key: string }) => header.key.toLowerCase() === "content-security-policy")
      .map((header: { value: string }) => header.value);
    expect(policies.length).toBeGreaterThan(0);
    for (const policy of policies) {
      const connect = policy.split(";").map((d) => d.trim()).find((d) => d.startsWith("connect-src")) ?? "";
      expect(connect.split(/\s+/)).toContain("https://api.trypotluck.ai");
    }
  });
});
