/** Account for every text, tool argument and schema without changing the payload. */
export function toolRequestTokens(body: Record<string, unknown>): number {
  const object = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  const fail = () => { throw new Error("Unsupported native-tool request"); };
  if (!Array.isArray(body.messages)) return fail();
  for (const message of body.messages) {
    if (!object(message) || !["system", "user", "assistant", "tool"].includes(String(message.role))) return fail();
    if (Object.keys(message).some(key => !["role", "content", "tool_calls", "tool_call_id", "name"].includes(key))) return fail();
    const content = message.content;
    if (content != null && typeof content !== "string" && !(Array.isArray(content) && content.every(part =>
      object(part) && part.type === "text" && typeof part.text === "string" && Object.keys(part).every(key => ["type", "text"].includes(key))))) return fail();
    if (message.role === "tool" && (typeof message.tool_call_id !== "string" || !message.tool_call_id)) return fail();
    if (message.tool_calls != null && (message.role !== "assistant" || !Array.isArray(message.tool_calls)
      || message.tool_calls.length > 64 || !message.tool_calls.every(call => object(call) && call.type === "function"
        && typeof call.id === "string" && object(call.function) && typeof call.function.name === "string"
        && typeof call.function.arguments === "string"))) return fail();
  }
  if (body.tools != null && (!Array.isArray(body.tools) || body.tools.length > 128 || !body.tools.every(tool =>
    object(tool) && tool.type === "function" && object(tool.function) && typeof tool.function.name === "string"
    && (tool.function.parameters == null || object(tool.function.parameters))))) return fail();
  // Like the plain-chat bridge, use a conservative byte estimate. Runtime
  // tokenization remains authoritative; a rejected request is never truncated.
  return 128 + body.messages.length * 16 + Buffer.byteLength(JSON.stringify({
    messages: body.messages, tools: body.tools, tool_choice: body.tool_choice,
  }), "utf8");
}
