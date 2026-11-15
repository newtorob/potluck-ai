import { createHash } from "node:crypto";

type Packet = {
  version: "potluck-shared-review-text-v1";
  source: string;
  candidate: { prefix: string; shared: "source"; suffix: string };
};
type Binding = { sourceSha256: string; candidateSha256: string };
const hash = (text: string) =>
  createHash("sha256").update(text, "utf8").digest("hex");
const keys = (
  value: unknown,
  expected: string[],
): value is Record<string, unknown> =>
  typeof value === "object" &&
  value !== null &&
  !Array.isArray(value) &&
  Object.keys(value).length === expected.length &&
  expected.every((key) => Object.hasOwn(value, key));

export function packSharedReviewText(
  sourceText: string,
  candidateText: string,
): { packet: Packet; binding: Binding } {
  if (
    typeof sourceText !== "string" ||
    typeof candidateText !== "string" ||
    !sourceText ||
    sourceText.includes("\0") ||
    candidateText.includes("\0") ||
    Buffer.byteLength(sourceText) > 16 * 1024 ||
    Buffer.byteLength(candidateText) > 32 * 1024 ||
    Buffer.from(sourceText).toString("utf8") !== sourceText ||
    Buffer.from(candidateText).toString("utf8") !== candidateText
  ) {
    throw new Error("Invalid review text");
  }
  const index = candidateText.indexOf(sourceText);
  if (index < 0 || candidateText.indexOf(sourceText, index + 1) !== -1)
    throw new Error("Source must occur exactly once");
  return {
    packet: {
      version: "potluck-shared-review-text-v1",
      source: sourceText,
      candidate: {
        prefix: candidateText.slice(0, index),
        shared: "source",
        suffix: candidateText.slice(index + sourceText.length),
      },
    },
    binding: {
      sourceSha256: hash(sourceText),
      candidateSha256: hash(candidateText),
    },
  };
}

export function restoreSharedReviewText(
  packet: unknown,
  binding: unknown,
): { sourceText: string; candidateText: string } {
  if (
    !keys(packet, ["version", "source", "candidate"]) ||
    packet.version !== "potluck-shared-review-text-v1" ||
    !keys(packet.candidate, ["prefix", "shared", "suffix"]) ||
    packet.candidate.shared !== "source" ||
    typeof packet.source !== "string" ||
    typeof packet.candidate.prefix !== "string" ||
    typeof packet.candidate.suffix !== "string" ||
    !keys(binding, ["sourceSha256", "candidateSha256"]) ||
    typeof binding.sourceSha256 !== "string" ||
    typeof binding.candidateSha256 !== "string"
  )
    throw new Error("Invalid review packet");
  const sourceText = packet.source;
  const candidateText =
    packet.candidate.prefix + sourceText + packet.candidate.suffix;
  const packed = packSharedReviewText(sourceText, candidateText);
  if (
    packed.binding.sourceSha256 !== binding.sourceSha256 ||
    packed.binding.candidateSha256 !== binding.candidateSha256 ||
    packed.packet.candidate.prefix !== packet.candidate.prefix ||
    packed.packet.candidate.suffix !== packet.candidate.suffix
  ) {
    throw new Error("Review packet binding mismatch");
  }
  return { sourceText, candidateText };
}
