import type { Interface } from "node:readline";

/** A cancelled or closed question returns null without consuming the next answer. */
export function createAsk(rl: Interface): (q: string, signal?: AbortSignal) => Promise<string | null> {
  let closed = false;
  rl.once("close", () => { closed = true; });
  return (q, signal) => new Promise(resolve => {
    if (closed || signal?.aborted) { resolve(null); return; }
    const finish = (answer: string | null): void => {
      rl.removeListener("close", cancel);
      signal?.removeEventListener("abort", cancel);
      resolve(answer);
    };
    const cancel = (): void => finish(null);
    rl.once("close", cancel);
    // readline removes its own question callback when this signal aborts.
    rl.question(q, { signal }, answer => finish(answer));
    signal?.addEventListener("abort", cancel, { once: true });
    if (signal?.aborted) cancel();
  });
}
