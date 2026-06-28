/**
 * A small, dependency-free unified diff for write_file confirmations.
 *
 * Line-based LCS. Output is plain text (no ANSI); the caller colorizes by line
 * prefix. Falls back to a coarse all-removed/all-added diff for very large
 * inputs to keep the O(n*m) table from blowing up.
 */

type Op = { type: "ctx" | "del" | "add"; line: string };

const MAX_CELLS = 4_000_000; // ~2000 x 2000 lines before we fall back

function splitLines(text: string): string[] {
  return text.length === 0 ? [] : text.split("\n");
}

function lcsOps(a: string[], b: string[]): Op[] {
  const n = a.length;
  const m = b.length;

  if (n * m > MAX_CELLS) {
    return [
      ...a.map((line): Op => ({ type: "del", line })),
      ...b.map((line): Op => ({ type: "add", line })),
    ];
  }

  // dp[i][j] = length of LCS of a[i:] and b[j:]
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array<number>(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ type: "ctx", line: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: "del", line: a[i] });
      i++;
    } else {
      ops.push({ type: "add", line: b[j] });
      j++;
    }
  }
  while (i < n) ops.push({ type: "del", line: a[i++] });
  while (j < m) ops.push({ type: "add", line: b[j++] });
  return ops;
}

/**
 * Render a unified-style diff for `path` going from `before` to `after`.
 * Returns a friendly note when there is nothing to change.
 */
export function unifiedDiff(path: string, before: string, after: string): string {
  if (before === after) return `(no changes to ${path})`;

  const ops = lcsOps(splitLines(before), splitLines(after));
  const header =
    before.length === 0
      ? `+++ ${path} (new file)`
      : `--- ${path}\n+++ ${path}`;

  const lines = ops.map((op) => {
    if (op.type === "add") return `+${op.line}`;
    if (op.type === "del") return `-${op.line}`;
    return ` ${op.line}`;
  });

  return `${header}\n${lines.join("\n")}`;
}
