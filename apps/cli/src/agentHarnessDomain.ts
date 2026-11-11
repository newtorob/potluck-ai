import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { isAbsolute, join, delimiter } from "node:path";
import type { ChatMessage } from "./gateway.js";

export type NativeResponseFormat = { type: "json_object"; schema: Record<string, unknown> };
export interface DomainPrepared { identity: string; binding: Record<string, unknown>; request: { messages: ChatMessage[]; response_format: NativeResponseFormat } }
export interface DomainDecoded { identity: string; result: Record<string, unknown> }
export interface DomainAdapter { prepare(sources: Record<string, string>): Promise<DomainPrepared>; decode(binding: Record<string, unknown>, raw: string): Promise<DomainDecoded> }
export class DomainRejected extends Error { constructor() { super("Domain input or result rejected"); this.name = "DomainRejected"; } }
export class DomainUnavailable extends Error { constructor() { super("Domain adapter unavailable"); this.name = "DomainUnavailable"; } }
const LIMIT = 256 * 1024;
const object = (x: unknown): x is Record<string, unknown> => !!x && typeof x === "object" && !Array.isArray(x);
const exact = (x: unknown, keys: string[]): x is Record<string, unknown> => object(x) && Object.keys(x).length === keys.length && keys.every(k => Object.hasOwn(x, k));
const identity = (x: unknown): x is string => typeof x === "string" && /^[a-f0-9]{64}$/.test(x);
export function validateDomainPrepared(x: unknown): DomainPrepared {
  if (!exact(x, ["identity", "binding", "request"]) || !identity(x.identity) || !object(x.binding) || !exact(x.request, ["messages", "response_format"])) throw new DomainUnavailable();
  const request = x.request;
  if (!Array.isArray(request.messages) || request.messages.length !== 2 || !request.messages.every((m, i) => exact(m, ["role", "content"]) && m.role === (i === 0 ? "system" : "user") && typeof m.content === "string") || !exact(request.response_format, ["type", "schema"]) || request.response_format.type !== "json_object" || !object(request.response_format.schema)) throw new DomainUnavailable();
  return x as unknown as DomainPrepared;
}
export function validateDomainDecoded(x: unknown): DomainDecoded {
  if (!exact(x, ["identity", "result"]) || !identity(x.identity) || !object(x.result)) throw new DomainUnavailable();
  return x as unknown as DomainDecoded;
}
export class PythonCoverageAdapter implements DomainAdapter {
  constructor(private readonly python: string, private readonly pythonRoot: string) {
    if (!isAbsolute(python) || !isAbsolute(pythonRoot) || pythonRoot.includes(delimiter)) throw new DomainUnavailable();
  }
  async prepare(sources: Record<string, string>): Promise<DomainPrepared> {
    if (!exact(sources, ["source-envelope.json", "draft.jsonl"]) || typeof sources["source-envelope.json"] !== "string" || typeof sources["draft.jsonl"] !== "string") throw new DomainRejected();
    return validateDomainPrepared(await this.call({ operation: "prepare", envelope_json: sources["source-envelope.json"], packet: sources["draft.jsonl"] }));
  }
  async decode(binding: Record<string, unknown>, raw: string): Promise<DomainDecoded> {
    if (!object(binding) || typeof raw !== "string") throw new DomainRejected();
    return validateDomainDecoded(await this.call({ operation: "decode", binding, raw }));
  }
  private call(value: unknown): Promise<unknown> {
    let input: string;
    try { input = JSON.stringify(value); } catch { return Promise.reject(new DomainRejected()); }
    if (Buffer.byteLength(input) > LIMIT) return Promise.reject(new DomainRejected());
    return new Promise((resolve, reject) => {
      const env: NodeJS.ProcessEnv = { PYTHONPATH: this.pythonRoot, PYTHONNOUSERSITE: "1", PYTHONDONTWRITEBYTECODE: "1", PYTHONPYCACHEPREFIX: join(tmpdir(), "potluck-no-cache-" + randomUUID()) };
      for (const key of ["PATH", "SystemRoot", "WINDIR", "LANG"]) if (process.env[key] !== undefined) env[key] = process.env[key];
      let child: ReturnType<typeof spawn>;
      try { child = spawn(this.python, ["-B", "-m", "potluck_local.services.investigation_agent_review_bridge"], { shell: false, cwd: this.pythonRoot, env, stdio: ["pipe", "pipe", "pipe"] }); } catch { reject(new DomainUnavailable()); return; }
      let stdout = Buffer.alloc(0), stderrBytes = 0, settled = false, forced = false;
      const finish = (error?: Error, result?: unknown) => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(result); };
      const stop = () => { forced = true; child.kill("SIGKILL"); };
      const timer = setTimeout(stop, 10000);
      child.on("error", () => finish(new DomainUnavailable()));
      child.stdin!.on("error", () => {});
      child.stdout!.on("data", (chunk: Buffer) => { if (stdout.length + chunk.length > LIMIT) { stop(); } else stdout = Buffer.concat([stdout, chunk]); });
      child.stderr!.on("data", (chunk: Buffer) => { stderrBytes += chunk.length; if (stderrBytes > LIMIT) { stop(); } });
      child.on("close", code => {
        if (forced) return finish(new DomainUnavailable());
        if (code === 2) return finish(new DomainRejected());
        if (code !== 0) return finish(new DomainUnavailable());
        try { finish(undefined, JSON.parse(stdout.toString("utf8"))); } catch { finish(new DomainUnavailable()); }
      });
      child.stdin!.end(input);
    });
  }
}
