import { createHash } from "node:crypto";

export interface FailureEvidence {
  excerpt: string;
  fingerprint: string;
  symbols: string[];
  locations: { path: string; line: number }[];
}

/** Diagnostic strings are data. Never infer missing expected values or execute paths. */
export function failureEvidence(output: string): FailureEvidence {
  const clean = output.replace(/\x1b\[[0-9;]*m/g, "");
  const lines = clean.split(/\r?\n/);
  const assertions = lines.filter(line => /^\s*(?:E\s|>\s)|AssertionError|\bassert\b|Expected:|Received:|expected .*got|actual[:=]/i.test(line));
  const locations: FailureEvidence["locations"] = [];
  for (const match of clean.matchAll(/File "([^"\n]+)", line (\d+)|(?:^|\n)\s*([^\s:]+\.(?:py|tsx?|jsx?)):(\d+)(?::\d+)?/g)) {
    const line = Number(match[2] ?? match[4]);
    if (Number.isSafeInteger(line) && line > 0) locations.push({ path: match[1] ?? match[3], line });
  }
  const symbols = [...new Set([...assertions.join("\n").matchAll(/\b([A-Za-z_$][\w$]{2,})\s*\(/g)].map(match => match[1]))].slice(0, 12);
  return { excerpt: compactVerification(clean, 2400),
    fingerprint: createHash("sha256").update((assertions.join("\n") || clean).slice(0, 16000)).digest("hex"),
    symbols, locations: locations.slice(-8) };
}

/** Keep literal failure evidence, not a model-written summary of the failure. */
export function compactVerification(output: string, limit = 3000): string {
  if (output.length <= limit) return output;
  const lines = output.split("\n");
  const wanted = new Set<number>();
  const add = (start: number, end: number) => {
    for (let i = Math.max(0, start); i < Math.min(lines.length, end); i++) wanted.add(i);
  };
  // Select assertions first so unrelated warnings cannot crowd out the failure.
  const anchors = lines.flatMap((line, i) => /^\s*(?:E\s|>\s)|AssertionError|\bassert\b|Expected:|Received:/.test(line) ? [i] : []);
  for (const i of anchors.slice(0, 16)) add(i - 2, i + 4);
  if (!anchors.length) add(0, 3);
  add(lines.length - 6, lines.length);
  for (let i = 0; i < lines.length; i++) {
    if (/^(?:FAIL(?:ED)?|ERROR)|File ".*", line|Traceback/.test(lines[i]) && wanted.size < 60) add(i, i + 2);
  }
  const marker = "[verification output excerpt; full output remains in the local transcript]\n";
  const selected = [...wanted].sort((a, b) => a - b);
  let text = "", previous = -1;
  for (const i of selected) {
    text += (i > previous + 1 ? "[lines omitted]\n" : "") + lines[i] + "\n";
    previous = i;
  }
  const room = limit - marker.length;
  if (text.length > room) {
    // Keep the first failure and the final test summary when there are many failures.
    const half = Math.floor((room - 20) / 2);
    // The cap is in UTF-16 characters, but never split a surrogate pair.
    const head = text.slice(0, half).replace(/[\uD800-\uDBFF]$/, "");
    const tail = text.slice(-half).replace(/^[\uDC00-\uDFFF]/, "");
    text = head + "\n[lines omitted]\n" + tail;
  }
  return marker + text;
}
