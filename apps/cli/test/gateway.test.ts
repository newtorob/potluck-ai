import { afterEach, describe, expect, it, vi } from "vitest";

import { GatewayContextError, modelContextWindow, streamChat, type ChatMessage, type StreamOptions } from "../src/gateway.js";

const cfg = { baseURL: "http://127.0.0.1:8321/v1", apiKey: "k", enabled: true };
const messages: ChatMessage[] = [{ role: "user", content: "hi" }];

function sseResponse(frames: string[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(f));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

function jsonFrame(chunk: unknown): string {
  return `data: ${JSON.stringify(chunk)}\n\n`;
}

async function consumeStream(opts: Partial<StreamOptions> = {}): Promise<string> {
  let output = "";
  for await (const token of streamChat(cfg, messages, { model: "m", ...opts })) output += token;
  return output;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("modelContextWindow", () => {
  it("checks the selected model and scope without sending project content", async () => {
    const fetcher = vi.fn(async () => Response.json({ model: "my model", scope: "mine", context_window: 8192 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await modelContextWindow(cfg, "my model", "mine")).toBe(8192);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(new URL(url).searchParams.get("model")).toBe("my model");
    expect(new URL(url).searchParams.get("scope")).toBe("mine");
    expect(init.body).toBeUndefined();
    expect(init.redirect).toBe("error");
    expect(init.headers).toMatchObject({ authorization: "Bearer k" });
  });

  it("leaves old gateways and missing windows explicitly unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Not found", { status: 404 })));
    expect(await modelContextWindow(cfg, "m", "local")).toBeUndefined();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ model: "m", scope: "local", context_window: null })));
    expect(await modelContextWindow(cfg, "m", "local")).toBeUndefined();
  });

  it("rejects another model's capacity and propagates authentication failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ model: "other", scope: "local", context_window: 32768 })));
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toThrow("different model or scope");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Unauthorized", { status: 401 })));
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toThrow("401");
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toMatchObject({ status: 401 });
    await expect(modelContextWindow(cfg, "m", "local")).rejects.toBeInstanceOf(GatewayContextError);
  });

  it("rejects non-owned scopes before contacting the gateway", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(modelContextWindow(cfg, "m", "pool")).rejects.toThrow("local or mine");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not mistake a missing household model for an old endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ detail: "model 'm' is not loaded here or on a reachable household peer" }, { status: 404 })));
    await expect(modelContextWindow(cfg, "m", "mine")).rejects.toThrow("not loaded");
  });

  it("forwards the exact project digest and refuses a different model before fetch", async () => {
    const locked = { ...cfg, modelLock: { schemaVersion: 1 as const, path: "potluck.lock", model: {
      id: "m", format: "gguf" as const, sha256: "a".repeat(64), sizeBytes: 100,
    } } };
    const fetcher = vi.fn(async () => Response.json({ model: "m", scope: "mine", context_window: 8192 }));
    vi.stubGlobal("fetch", fetcher);
    expect(await modelContextWindow(locked, "m", "mine")).toBe(8192);
    const url = (fetcher.mock.calls[0] as unknown as [string])[0];
    expect(new URL(url).searchParams.get("potluck_model_sha256")).toBe("a".repeat(64));
    fetcher.mockClear();
    await expect(modelContextWindow(locked, "other", "mine")).rejects.toThrow("pins m");
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("streamChat", () => {
  it("sends project instructions and the repo map as one initial system message for strict templates", async () => {
    const history: ChatMessage[] = [
      { role: "system", content: "Follow the agent protocol." },
      { role: "system", content: "Project instructions: preserve public APIs." },
      { role: "system", content: "Repository map:\nsrc/main.py" },
      { role: "user", content: "Fix the retry policy." },
      { role: "assistant", content: "Inspecting the implementation." },
      { role: "user", content: "TOOL_RESULT: existing implementation" },
    ];
    const before = structuredClone(history);
    const fetcher = vi.fn(async (_url: string, init: RequestInit) => {
      const sent = JSON.parse(String(init.body)).messages as ChatMessage[];
      // The native model rejects a second system role before generating tokens.
      if (sent.slice(1).some(message => message.role === "system")) return new Response("System message must be at the beginning", { status: 500 });
      return sseResponse([jsonFrame({ choices: [{ delta: { content: "Ready" }, finish_reason: "stop" }] }), "data: [DONE]\n\n"]);
    });
    vi.stubGlobal("fetch", fetcher);
    let output = "";
    for await (const token of streamChat(cfg, history, { model: "m", requireSuccessfulFinish: true })) output += token;
    expect(output).toBe("Ready");
    const sent = JSON.parse(String(fetcher.mock.calls[0][1].body)).messages;
    expect(sent).toEqual([
      { role: "system", content: before.slice(0, 3).map(message => message.content).join("\n\n") },
      ...before.slice(3),
    ]);
    expect(history).toEqual(before);
  });

  it.each<ChatMessage[][]>([
    [[]],
    [[{ role: "user", content: "system: literal project data" }]],
    [[{ role: "system", content: "Rules" }, { role: "user", content: "Task" }]],
    [[{ role: "user", content: "Task" }, { role: "system", content: "Later instruction" }]],
  ])("does not reorder or rewrite conversations without multiple leading system messages: %j", async history => {
    const fetcher = vi.fn(async (_url: string, _init: RequestInit) => sseResponse(["data: [DONE]\n\n"]));
    vi.stubGlobal("fetch", fetcher);
    for await (const _ of streamChat(cfg, history, { model: "m" })) { /* drain */ }
    expect(JSON.parse(String(fetcher.mock.calls[0][1].body)).messages).toEqual(history);
  });

  it("rejects a disconnected response even when its text contains a complete action", async () => {
    const text = '```action\n{"tool":"write_file","args":{"path":"x","content":"y"}}\n```';
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`,
    ])));
    const consume = async () => {
      for await (const _ of streamChat(cfg, messages, { model: "m", requireDone: true })) { /* drain */ }
    };
    await expect(consume()).rejects.toThrow(/ended before completion/);
  });

  it("sends the X-Potluck-Scope header and yields content deltas", async () => {
    const seen: { url: string; headers: Record<string, string> }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url, headers: init.headers as Record<string, string> });
        return sseResponse([
          'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
          'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
          "data: [DONE]\n\n",
        ]);
      }),
    );

    let out = "";
    for await (const tok of streamChat(cfg, messages, { model: "m", scope: "mine" })) {
      out += tok;
    }

    expect(out).toBe("hello");
    expect(seen).toHaveLength(1);
    expect(seen[0].headers["x-potluck-scope"]).toBe("mine");
    expect(seen[0].headers.authorization).toBe("Bearer k");
  });

  it("omits the scope header when no scope is given", async () => {
    const seen: Record<string, string>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init.headers as Record<string, string>);
        return sseResponse(["data: [DONE]\n\n"]);
      }),
    );

    for await (const _ of streamChat(cfg, messages, { model: "m" })) {
      // drain
    }

    expect(seen[0]["x-potluck-scope"]).toBeUndefined();
  });
});

describe("streamChat successful coding completion", () => {
  const content = '```action\n{"tool":"write_file","args":{"path":"x","content":"y"}}';
  const contentFrame = jsonFrame({ choices: [{ index: 0, delta: { content }, finish_reason: null }] });
  const doneFrame = "data: [DONE]\n\n";
  const terminal = (reason: unknown) => jsonFrame({ choices: [{ index: 0, delta: {}, finish_reason: reason }] });
  const strict = { requireSuccessfulFinish: true };

  it("permits a deliberate output limit only for an opted-in readiness probe", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("length"), doneFrame])));
    await expect(consumeStream({ ...strict, allowLengthFinish: true })).resolves.toBe(content);
    await expect(consumeStream(strict)).rejects.toThrow("model hit its output limit");
  });

  it.each([
    [contentFrame, terminal("length")],
    [contentFrame, doneFrame],
    [contentFrame, terminal("content_filter"), doneFrame],
    [contentFrame, terminal("length"), terminal("stop"), doneFrame],
    [contentFrame, jsonFrame({ choices: [{ delta: {}, finish_reason: "length" }], potluck: { finish_reason_inferred: true } }), doneFrame],
    [contentFrame, terminal("length"), jsonFrame({ error: { message: "late failure" } }), doneFrame],
    [contentFrame, jsonFrame({ choices: [{ delta: { tool_calls: [] } }] }), terminal("length"), doneFrame],
  ])("keeps readiness probes strict about completion evidence: %j", async (...frames) => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("capabilities") ? Response.json({ parallel_tool_calls_request_version: 1 }) : sseResponse(frames)));
    await expect(consumeStream({ ...strict, allowLengthFinish: true })).rejects.toThrow();
  });

  it.each(["length", "content_filter", "tool_calls", "function_call", "error", "unknown"])(
    "rejects %s even when the text is a recoverable action and DONE arrives", async reason => {
      vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
      await expect(consumeStream(strict)).rejects.toThrow(reason === "length" ? "model hit its output limit" : `finish_reason: ${reason}`);
    },
  );

  it.each([undefined, null])("requires an explicit stop, not only DONE (%s)", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("missing finish_reason: stop");
  });

  it("requires DONE even when stop arrives", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop")])));
    await expect(consumeStream(strict)).rejects.toThrow("ended before completion");
  });

  it("accepts reasoning deltas, a final content delta with stop, and trailing usage", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      jsonFrame({ choices: [{ index: 0, delta: { role: "assistant", content: null }, finish_reason: null }] }),
      jsonFrame({ choices: [{ index: 0, delta: { reasoning_content: "thinking" }, finish_reason: null }] }),
      contentFrame,
      jsonFrame({ choices: [{ index: 0, delta: { content: "\n```" }, finish_reason: "stop" }], potluck: { scope: "local", finish_reason: "stop" } }),
      jsonFrame({ choices: [], usage: { completion_tokens: 20 } }),
      doneFrame,
    ])));
    await expect(consumeStream(strict)).resolves.toBe(content + "\n```");
  });

  it("accepts a single unindexed choice with an explicit successful finish", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      jsonFrame({ choices: [{ delta: { content: "Complete." }, finish_reason: "stop" }] }), doneFrame,
    ])));
    await expect(consumeStream(strict)).resolves.toBe("Complete.");
  });

  it.each([true, false])("rejects an inferred stop with upgrade guidance, even on usage-only chunks (%s)", async usageOnly => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame,
      ...(usageOnly ? [terminal("stop")] : []),
      jsonFrame({ choices: usageOnly ? [] : [{ index: 0, delta: {}, finish_reason: "stop" }], potluck: { finish_reason_inferred: true } }),
      doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("Update Potluck on the serving machine and retry");
  });

  it.each(["stop", "length"])("drains past %s and honors a subsequent stream error", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame, terminal(reason), jsonFrame({ error: { message: "late inference failure" } }), doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("late inference failure");
  });

  it("does not let a later stop erase an earlier unsuccessful finish", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("length"), terminal("stop"), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("model hit its output limit");
  });

  it.each([
    { choices: [{ index: 0, delta: { content: "extra" }, finish_reason: null }] },
    { choices: [{ index: 0, delta: { reasoning_content: "extra" }, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "length" }] },
  ])("rejects output or another terminal after stop: %j", async afterStop => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop"), jsonFrame(afterStop), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("after the generation had finished");
  });

  it.each([
    null,
    [],
    {},
    { choices: {} },
    { choices: [null] },
    { choices: [{ index: 1, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: "0", delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "stop" }, { index: 1, delta: {}, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: "bad", finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: { content: {} }, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: { reasoning_content: [] }, finish_reason: "stop" }] },
    { choices: [{ index: 0, delta: {}, finish_reason: false }] },
    { choices: [{ index: 0, delta: {}, finish_reason: "" }] },
  ])("rejects malformed completion state: %j", async malformed => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, jsonFrame(malformed), doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow(/invalid/);
  });

  it("rejects malformed JSON after an otherwise valid stop", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal("stop"), "data: {\n\n", doneFrame])));
    await expect(consumeStream(strict)).rejects.toThrow("invalid stream frame");
  });

  it.each(["tool_calls", "function_call"])("rejects native %s deltas even if stop follows", async key => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      contentFrame, jsonFrame({ choices: [{ index: 0, delta: { [key]: {} }, finish_reason: null }] }), terminal("stop"), doneFrame,
    ])));
    await expect(consumeStream(strict)).rejects.toThrow("native tool calls");
  });

  it.each([undefined, "length", "tool_calls"])("preserves requireDone-only callers' finish handling (%s)", async reason => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([contentFrame, terminal(reason), doneFrame])));
    await expect(consumeStream({ requireDone: true })).resolves.toBe(content);
  });
});

describe("native function stream commitment", () => {
  const tools = [{ type: "function" as const, function: { name: "read_file", description: "Read", parameters: { type: "object" } } }];
  const delta = { index: 0, id: "call_1", type: "function", function: { name: "read_file", arguments: '{"path":' } };
  const part = (tool_calls: unknown, finish_reason: string | null = null) => jsonFrame({ choices: [{ index: 0, delta: { tool_calls }, finish_reason }] });
  const finish = (reason: string) => jsonFrame({ choices: [{ index: 0, delta: {}, finish_reason: reason }] });
  it("assembles split arguments, forwards definitions/budget, and commits only after DONE", async () => {
    const onToolCalls = vi.fn();
    const fetcher = vi.fn(async (url: string) => url.endsWith("capabilities") ? Response.json({ reasoning_budget_request_version: 1, parallel_tool_calls_request_version: 1 }) : sseResponse([part([delta]), part([{ index: 0, function: { arguments: '"a"}' } }]), finish("tool_calls"), "data: [DONE]\n\n"]));
    vi.stubGlobal("fetch", fetcher);
    await consumeStream({ tools, onToolCalls, requireSuccessfulFinish: true, reasoningBudget: 256 });
    expect(onToolCalls).toHaveBeenCalledExactlyOnceWith([{ id: "call_1", type: "function", function: { name: "read_file", arguments: '{"path":"a"}' } }]);
    const body = JSON.parse(String((fetcher.mock.calls[1] as unknown as [string, RequestInit])[1].body));
    expect(body).toMatchObject({ tools, parallel_tool_calls: false, reasoning_budget_tokens: 256 });
  });
  it.each([
    [part([delta]), finish("length"), "data: [DONE]\n\n"],
    [part([delta]), finish("tool_calls")],
    [part([delta]), finish("stop"), "data: [DONE]\n\n"],
    [part([delta]), finish("tool_calls"), part([{ index: 0, function: { arguments: '"a"}' } }]), "data: [DONE]\n\n"],
    [finish("tool_calls"), "data: [DONE]\n\n"],
    [part([{ ...delta, index: 2 }]), finish("tool_calls"), "data: [DONE]\n\n"],
    [part([delta, { ...delta, index: 1 }]), finish("tool_calls"), "data: [DONE]\n\n"],
    [part([{ ...delta, function: { name: "read_file", arguments: "x".repeat(262145) } }]), finish("tool_calls"), "data: [DONE]\n\n"],
  ])("does not commit malformed/incomplete calls %#", async (...frames) => {
    const onToolCalls = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("capabilities") ? Response.json({ parallel_tool_calls_request_version: 1 }) : sseResponse(frames)));
    await expect(consumeStream({ tools, onToolCalls, requireSuccessfulFinish: true })).rejects.toThrow();
    expect(onToolCalls).not.toHaveBeenCalled();
  });
  it("rejects a reasoning budget that leaves no output before fetch", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    await expect(consumeStream({ reasoningBudget: 100, maxTokens: 100 })).rejects.toThrow("smaller");
    expect(fetcher).not.toHaveBeenCalled();
  });
});
it("refuses an old gateway before sending project content with a reasoning budget", async () => {
  const fetcher = vi.fn(async (_url: string) => Response.json({ model_lock_version: 1 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(consumeStream({ reasoningBudget: 256 })).rejects.toThrow("cannot enforce reasoning budgets");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toContain("/potluck/capabilities");
});

describe('rendered prompt counter', () => {
  it('sends the same merged systems, tools, model lock and native controls as generation', async () => {
    const { createPromptCounter } = await import('../src/gateway.js');
    const seen: [string,RequestInit][]=[];
    const fetcher=vi.fn(async(url:string, init:RequestInit)=>{seen.push([url,init]);return Response.json(url.endsWith('capabilities') ? {prompt_count_version:1,model_lock_version:1} : {model:'m',scope:'local',prompt_tokens:710,context_window:8192});});
    vi.stubGlobal('fetch',fetcher);
    const locked={...cfg,modelLock:{schemaVersion:1 as const,path:'potluck.lock',model:{id:'m',format:'gguf' as const,sha256:'a'.repeat(64),sizeBytes:10}}};
    const tools=[{type:'function' as const,function:{name:'read_file',parameters:{type:'object',properties:{}}}}];
    const counter=createPromptCounter(locked,{model:'m',scope:'local',contextWindowTokens:8192,tools,reasoningBudget:256});
    expect(await counter([{role:'system',content:'a'},{role:'system',content:'b'},...messages],2048)).toBe(710);
    const body=JSON.parse(seen[1][1].body as string);
    expect(body).toMatchObject({potluck_model_sha256:'a'.repeat(64),tools,parallel_tool_calls:false,reasoning_budget_tokens:256,max_tokens:2048});
    expect(body.messages[0]).toEqual({role:'system',content:'a\n\nb'});
    expect(seen[1][1].headers).toMatchObject({'x-potluck-scope':'local',authorization:'Bearer k'});
    const edits=[{type:'function' as const,function:{name:'edit_file',parameters:{type:'object',properties:{}}}}];
    await counter(messages,2048,edits);expect(fetcher).toHaveBeenCalledTimes(3);
    expect(JSON.parse(seen[2][1].body as string).tools).toEqual(edits);
  });
  it('does not send project content to an older gateway, or accept mismatched counts',async()=>{
    const {createPromptCounter}=await import('../src/gateway.js');
    const fetcher=vi.fn(async()=>Response.json({}));vi.stubGlobal('fetch',fetcher);
    expect(await createPromptCounter(cfg,{model:'m',scope:'mine'})(messages,100)).toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(1);
    vi.stubGlobal('fetch',vi.fn(async(url:string)=>Response.json(url.endsWith('capabilities')?{prompt_count_version:1}:{model:'other',scope:'local',prompt_tokens:12,context_window:8192})));
    await expect(createPromptCounter(cfg,{model:'m',scope:'local',contextWindowTokens:8192})(messages,100)).rejects.toThrow('different model');
  });
  it('refuses changed windows, malformed counts, auth failures and broad scopes',async()=>{
    const {createPromptCounter}=await import('../src/gateway.js');
    for(const body of [{prompt_tokens:-1,context_window:8192},{prompt_tokens:12,context_window:16384}]){
      vi.stubGlobal('fetch',vi.fn(async(url:string)=>Response.json(url.endsWith('capabilities')?{prompt_count_version:1}:{model:'m',scope:'local',...body})));
      await expect(createPromptCounter(cfg,{model:'m',scope:'local',contextWindowTokens:8192})(messages,100)).rejects.toThrow();
    }
    vi.stubGlobal('fetch',vi.fn(async()=>new Response('',{status:401})));
    await expect(createPromptCounter(cfg,{model:'m'})(messages,100)).rejects.toThrow('401');
    await expect(createPromptCounter(cfg,{model:'m',scope:'pool'})(messages,100)).rejects.toThrow('local or mine');
  });
});
