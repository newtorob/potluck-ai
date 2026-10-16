import { createInterface } from "node:readline";
import { PassThrough } from "node:stream";
import { expect, it } from "vitest";
import { createAsk } from "../src/prompt.js";

it("cancels an approval prompt and accepts a fresh follow-up answer", async () => {
  const input = new PassThrough(), output = new PassThrough();
  const rl = createInterface({ input, output, terminal: false });
  try {
    const ask = createAsk(rl), cancel = new AbortController();
    const approval = ask("Approve?", cancel.signal);
    cancel.abort(); expect(await approval).toBeNull();
    const followup = ask("Next task?");
    input.write("corrected task\n");
    expect(await followup).toBe("corrected task");
  } finally { rl.close(); input.destroy(); output.destroy(); }
});
it("returns null on a closed prompt or already cancelled approval", async () => {
  const input = new PassThrough(), output = new PassThrough();
  const rl = createInterface({ input, output, terminal: false });
  const ask = createAsk(rl), cancel = new AbortController(); cancel.abort();
  expect(await ask("Approve?", cancel.signal)).toBeNull();
  const waiting = ask("Next?"); rl.close();
  expect(await waiting).toBeNull(); expect(await ask("After close?")).toBeNull();
  input.destroy(); output.destroy();
});
