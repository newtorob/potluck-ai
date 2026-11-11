export interface SourceLocation { name: string; line: number }

/** Bounded lexical navigation, not a call graph or proof that a symbol is faulty. */
export function sourceNavigation(source: string, observed: string, focus: readonly string[]): SourceLocation[] {
  const calls = new Set([...observed.matchAll(/\b([A-Za-z_$][\w$]{2,})\s*\(/g)].map(match => match[1]));
  const names = new Set(focus);
  const candidates: (SourceLocation & { score: number })[] = [];
  for (const [index, line] of source.split(/\r?\n/).entries()) {
    const match = line.match(/^\s*(?:(?:export|default|async|public|private|static)\s+)*(?:def|class|function|fn|func)\s+([A-Za-z_$][\w$]*)\b/)
      ?? line.match(/^\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>/);
    if (match && (names.has(match[1]) || calls.has(match[1]))) candidates.push({ name: match[1], line: index + 1, score: Number(names.has(match[1])) * 2 + Number(calls.has(match[1])) });
  }
  return candidates.sort((a, b) => b.score - a.score || a.line - b.line).slice(0, 8).map(({ name, line }) => ({ name, line }));
}
