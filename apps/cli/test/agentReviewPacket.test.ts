import { describe, expect, it } from "vitest";
import {
  packSharedReviewText,
  restoreSharedReviewText,
} from "../src/agentReviewPacket.js";

describe("shared review text", () => {
  it("round trips Unicode, CRLF, and source conditions exactly", () => {
    const source =
      "🍵 SES: under 14/s\r\nShare if both stacks share the account limit.\r\n";
    const candidate = `Review:\r\n${source}Status: open, unverified.\r\n`;
    const { packet, binding } = packSharedReviewText(source, candidate);
    expect(restoreSharedReviewText(packet, binding)).toEqual({
      sourceText: source,
      candidateText: candidate,
    });
  });
  it("rejects empty, missing, repeated, overlapping, and NUL text", () => {
    for (const [source, candidate] of [
      ["", "a"],
      ["x", "a"],
      ["x", "xx"],
      ["aa", "aaa"],
      ["x\0", "x\0"],
      ["x", "x\0"],
    ]) {
      expect(() => packSharedReviewText(source, candidate)).toThrow();
    }
  });
  it("enforces UTF-8 byte limits", () => {
    expect(() =>
      packSharedReviewText("é".repeat(8192), "é".repeat(8192)),
    ).not.toThrow();
    expect(() =>
      packSharedReviewText("é".repeat(8193), "é".repeat(8193)),
    ).toThrow();
    expect(() =>
      packSharedReviewText("x", "x" + "a".repeat(32767)),
    ).not.toThrow();
    expect(() => packSharedReviewText("x", "x" + "a".repeat(32768))).toThrow();
  });
  it("rejects malformed Unicode instead of hashing replacement characters", () => {
    expect(() => packSharedReviewText("\ud800", "\ud800")).toThrow();
    expect(() => packSharedReviewText("source", "source\udfff")).toThrow();
  });
  it("rejects corrupted fields, bindings, versions and extra fields", () => {
    const { packet, binding } = packSharedReviewText(
      "source",
      "before source after",
    );
    for (const bad of [
      { ...packet, version: "v2" },
      { ...packet, extra: true },
      { ...packet, candidate: { ...packet.candidate, extra: true } },
      { ...packet, candidate: { ...packet.candidate, suffix: " changed" } },
      { ...packet, candidate: { ...packet.candidate, shared: "other" } },
    ]) {
      expect(() => restoreSharedReviewText(bad, binding)).toThrow();
    }
    expect(() =>
      restoreSharedReviewText(packet, { ...binding, sourceSha256: "bad" }),
    ).toThrow();
    expect(() =>
      restoreSharedReviewText(packet, { ...binding, extra: true }),
    ).toThrow();
    expect(() => restoreSharedReviewText(null, binding)).toThrow();
  });
  it("serializes a long source only once", () => {
    const source = "condition ".repeat(500);
    const candidate = `before ${source} after`;
    const { packet, binding } = packSharedReviewText(source, candidate);
    expect(JSON.stringify({ packet, binding }).length).toBeLessThan(
      JSON.stringify({ source, candidate }).length,
    );
    expect(restoreSharedReviewText(packet, binding)).toEqual({
      sourceText: source,
      candidateText: candidate,
    });
  });
});
