import { resolve } from "node:path";
import { existsSync } from "node:fs";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { describe, expect, it, vi } from "vitest";
import { DomainRejected, DomainUnavailable, PythonCoverageAdapter, validateDomainDecoded, validateDomainPrepared } from "../src/agentHarnessDomain.js";
const hash = "a".repeat(64);
const prepared = () => ({ identity: hash, binding: {}, request: { messages: [{ role: "system", content: "system" }, { role: "user", content: "user" }], response_format: { type: "json_object", schema: { type: "object" } } } });
const root = resolve("../desktop/python"), python = resolve(root, ".venv/bin/python");
const fixture = () => {
  const text = "Records expire after thirty days. Managers approve access.", quote = "Managers approve access.", start = text.indexOf(quote);
  const envelope = { version: "focused-revision-v6", scope: "cited", group: { id: "G1", focus: "How long are records kept?", request: "How long are records kept?", status: "unknown", draft_sentences: ["private draft"] }, sources: [{ id: "Q1", snippet_id: "S1", quote, context: { citation_id: "CIT1", heading: "Retention", list_introduction: null, cited_chunk: { quote: text, selected_start: start, selected_end: start + quote.length } } }] };
  const packet = [{ record: "answer", question: "How long are records kept?", verdict: "source_rule", selected_ids: ["U2"], rule: "Selected units count.", covered_by_exact_binding_ids: [] }, { record: "source", id: "C1", headings: ["Retention"], retained_bindings: [] }, { id: "U1", state: "supplied_only", text: "Records expire after thirty days. " }, { id: "U2", state: "selected", text: "Managers approve access." }].map(x => JSON.stringify(x)).join("\n");
  return { "source-envelope.json": JSON.stringify(envelope), "draft.jsonl": packet };
};
describe("domain adapter", () => {
  it("requires exact response shapes and ordered system/user messages", () => {
    expect(validateDomainPrepared(prepared()).identity).toBe(hash);
    expect(validateDomainDecoded({ identity: hash, result: {} }).identity).toBe(hash);
    for (const value of [null, { ...prepared(), extra: true }, { ...prepared(), identity: "bad" }, { ...prepared(), binding: [] }, { ...prepared(), request: { ...prepared().request, messages: [{ role: "user", content: "x" }] } }, { ...prepared(), request: { ...prepared().request, response_format: { type: "json_object", schema: [] } } }]) expect(() => validateDomainPrepared(value)).toThrow(DomainUnavailable);
    expect(() => validateDomainDecoded({ identity: hash, result: [], extra: true })).toThrow(DomainUnavailable);
  });
  it("rejects malformed source inputs and oversized requests", async () => {
    const a = new PythonCoverageAdapter(python, root);
    await expect(a.prepare({ "source-envelope.json": "{", "draft.jsonl": "x" })).rejects.toBeInstanceOf(DomainRejected);
    await expect(a.prepare({ ...fixture(), extra: "x" })).rejects.toBeInstanceOf(DomainRejected);
    await expect(a.prepare({ ...fixture(), "draft.jsonl": "x".repeat(262145) })).rejects.toBeInstanceOf(DomainRejected);
    await expect(a.decode({}, "x".repeat(262145))).rejects.toBeInstanceOf(DomainRejected);
  });
  it.skipIf(!existsSync(python))("runs the fixed Python bridge and maps valid and rejected results", async () => {
    const a = new PythonCoverageAdapter(python, root), result = await a.prepare(fixture());
    expect(result.identity).toMatch(/^[a-f0-9]{64}$/);
    expect(result.request.messages.map(m => m.role)).toEqual(["system", "user"]);
    expect(JSON.stringify(result.request)).not.toContain("private draft");
    const raw = JSON.stringify({ verdict: "source_rule", units: { U1: "answer", U2: "background" }, dependencies: [], comparisons: [] });
    const decoded = await a.decode(result.binding, raw);
    expect(decoded.identity).toBe(result.identity);
    expect(decoded.result).toMatchObject({ status: "needs_review", findings: [{ unit_id: "U1", quote: "Records expire after thirty days. " }] });
    await expect(a.decode(result.binding, "{}")).rejects.toBeInstanceOf(DomainRejected);
    const duplicate = fixture(); duplicate["source-envelope.json"] = duplicate["source-envelope.json"].replace('"status":"unknown"', '"status":"unknown","status":"supported"');
    await expect(a.prepare(duplicate)).rejects.toBeInstanceOf(DomainRejected);
  });
  it.skipIf(!existsSync(python))("bounds output and isolates environment while mapping subprocess exits", async () => {
    const dir = await mkdtemp(resolve(tmpdir(), "domain-adapter-"));
    const service = resolve(dir, "potluck_local/services");
    const key = "POTLUCK_TEST_SECRET_NOT_FOR_CHILD"; process.env[key] = "secret";
    try {
      await mkdir(service, { recursive: true });
      await writeFile(resolve(service, "investigation_agent_review_bridge.py"), `import json, os, sys
x=json.load(sys.stdin)
mode=x['envelope_json']
if mode=='exit2': sys.stderr.write('secret detail'); sys.exit(2)
if mode=='exit1': sys.stderr.write('secret detail'); sys.exit(1)
if mode=='large': sys.stdout.write('x'*262145); sys.exit(0)
if mode=='stderr': sys.stderr.write('x'*262145); sys.exit(0)
if os.getenv('${key}'): sys.exit(1)
print(json.dumps({'identity':'a'*64,'binding':{},'request':{'messages':[{'role':'system','content':'s'},{'role':'user','content':'u'}],'response_format':{'type':'json_object','schema':{}}}}))
`);
      const a = new PythonCoverageAdapter(python, dir);
      const input = (mode: string) => ({ "source-envelope.json": mode, "draft.jsonl": "x" });
      expect((await a.prepare(input("ok"))).identity).toBe(hash);
      await expect(a.prepare(input("exit2"))).rejects.toBeInstanceOf(DomainRejected);
      for (const mode of ["exit1", "large", "stderr"]) await expect(a.prepare(input(mode))).rejects.toThrow("Domain adapter unavailable");
      vi.useFakeTimers();
      try {
        const timed = expect(a.prepare(input("ok"))).rejects.toBeInstanceOf(DomainUnavailable);
        await vi.advanceTimersByTimeAsync(10000); await timed;
      } finally { vi.useRealTimers(); }
    } finally { delete process.env[key]; await rm(dir, { recursive: true, force: true }); }
  });
  it("reports missing executables without exposing subprocess data", async () => {
    expect(() => new PythonCoverageAdapter("relative", root)).toThrow(DomainUnavailable);
    await expect(new PythonCoverageAdapter("/missing/potluck-python", root).prepare(fixture())).rejects.toThrow("Domain adapter unavailable");
  });
});
