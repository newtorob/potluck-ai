/** Collect structured calls without authorizing execution before the terminal SSE frame. */
export interface NativeToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface FunctionTool {
  type: "function";
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

export class NativeToolStream {
  private calls = new Map<number, NativeToolCall>();
  private bytes = 0;

  append(value: unknown): void {
    if (!Array.isArray(value) || value.length === 0) throw new Error("Gateway returned invalid native tool deltas.");
    for (const delta of value) {
      if (!delta || typeof delta !== "object" || !Number.isSafeInteger(delta.index) || delta.index < 0 || delta.index >= 8) {
        throw new Error("Gateway returned an invalid native tool index.");
      }
      const call = this.calls.get(delta.index) ?? { id: "", type: "function", function: { name: "", arguments: "" } };
      if (delta.type !== undefined && delta.type !== "function") throw new Error("Only function tools are supported.");
      if (delta.id !== undefined) {
        if (typeof delta.id !== "string" || !/^[\w-]{1,128}$/.test(delta.id) || call.id && call.id !== delta.id) {
          throw new Error("Gateway changed or omitted a native tool identity.");
        }
        call.id = delta.id;
      }
      if (delta.function !== undefined) {
        if (!delta.function || typeof delta.function !== "object" || Array.isArray(delta.function)) {
          throw new Error("Gateway returned an invalid function delta.");
        }
        for (const key of ["name", "arguments"] as const) {
          if (delta.function[key] === undefined) continue;
          if (typeof delta.function[key] !== "string") throw new Error("Gateway returned invalid function text.");
          this.bytes += Buffer.byteLength(delta.function[key]);
          if (this.bytes > 256 * 1024) throw new Error("Gateway native tool arguments are too large.");
          call.function[key] += delta.function[key];
        }
      }
      this.calls.set(delta.index, call);
    }
  }

  finish(reason: string | undefined): NativeToolCall[] {
    const calls = [...this.calls.entries()].sort(([a], [b]) => a - b);
    if (calls.length === 0 && reason !== "tool_calls") return [];
    if (reason !== "tool_calls" || calls.length === 0) throw new Error("Gateway did not confirm complete native tool calls.");
    const ids = new Set<string>();
    return calls.map(([index, call], position) => {
      if (index !== position || !call.id || ids.has(call.id) || !/^[A-Za-z_][\w-]{0,63}$/.test(call.function.name) || !call.function.arguments) {
        throw new Error("Gateway returned an incomplete or duplicate native tool call.");
      }
      ids.add(call.id);
      return call;
    });
  }
}
