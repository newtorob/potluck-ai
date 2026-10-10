import { describe, expect, it } from "vitest";
import { parseArgs } from "../src/args.js";

describe("command dispatch", () => {
  it("retains REPL, one-shot and setup entry points", () => {
    expect(parseArgs([]).command).toBeUndefined();
    expect(parseArgs(["-p", "fix the build", "--yes", "--scope=local", "-m", "coder"]))
      .toMatchObject({ print: true, task: "fix the build", yes: true, scope: "local", model: "coder" });
    expect(parseArgs(["setup", "--help"])).toMatchObject({ command: "setup", help: true });
  });
  it("parses diagnostics and bounded log options", () => {
    expect(parseArgs(["doctor", "--json", "--timeout=700", "--scope", "mine"]))
      .toMatchObject({ command: "doctor", json: true, timeout: 700, scope: "mine" });
    expect(parseArgs(["logs", "--source", "mesh", "--lines", "30"]))
      .toMatchObject({ command: "logs", source: "mesh", lines: 30 });
  });
  it.each([
    ["sttaus"], ["status", "extra"], ["--bogus"], ["--model"], ["-p"],
    ["doctor", "--timeout", "0"], ["doctor", "--timeout", "30001"],
    ["logs", "--lines", "2.5"], ["logs", "--lines", "1001"],
    ["logs", "--source", "secrets"], ["status", "--source", "mesh"],
    ["status", "--model", "coder"], ["doctor", "--scope", "pool"],
    ["setup", "--preferences", "--json"], ["--json"], ["--verify", "test", "--no-verify"],
    ["status", "-p", "hello"],
  ])("rejects invalid invocation %j", (...args) => {
    expect(() => parseArgs(args)).toThrow();
  });
  it("parses explicit setup and account options", () => {
    expect(parseArgs(["setup", "--no-input", "--enable-gateway", "--connect", "--name", "Server"]))
      .toMatchObject({ noInput: true, enableGateway: true, connect: true, name: "Server" });
    expect(parseArgs(["login", "--no-wait", "--json"]).wait).toBe(false);
    expect(parseArgs(["login", "--no-browser"]).browser).toBe(false);
    expect(parseArgs(["setup", "--sign-in", "--no-browser"]).browser).toBe(false);
    expect(parseArgs(["setup", "--json"]).json).toBe(true);
  });
  it.each([["login", "--wait", "--no-wait"], ["household", "invite"], ["login", "secret"],
    ["status", "--sign-in"], ["connect", "--output", "secret"], ["household", "list", "--name", "x"],
    ["connect", "--name", "\u001bsecret"], ["--no-input"], ["status", "--no-browser"]])("rejects misplaced account options %j", (...args) => {
    expect(() => parseArgs(args)).toThrow();
  });
});

describe("benchmark options", () => {
  it("parses the model, runs, switch handling, scope and output options", () => {
    expect(parseArgs(["benchmark"])).toMatchObject({ command: "benchmark", timeout: 120000, yes: false, json: false });
    expect(parseArgs(["benchmark", "--model", "qwen3-8b-q4", "--runs", "5", "--keep", "--yes", "--scope", "mine", "--json", "--timeout", "300000"]))
      .toMatchObject({ command: "benchmark", model: "qwen3-8b-q4", runs: 5, keep: true, yes: true, scope: "mine", json: true, timeout: 300000 });
    expect(parseArgs(["benchmark", "--restore", "-m", "small", "-y", "--runs=10", "--scope=local"]))
      .toMatchObject({ restore: true, model: "small", yes: true, runs: 10, scope: "local" });
    expect(parseArgs(["benchmark", "--help"])).toMatchObject({ command: "benchmark", help: true });
  });
  it.each([
    ["benchmark", "--runs", "0"], ["benchmark", "--runs", "11"], ["benchmark", "--runs", "2.5"], ["benchmark", "--runs", "many"], ["benchmark", "--runs"],
    ["benchmark", "--keep", "--restore"], ["benchmark", "extra"], ["benchmark", "--model", "../private"], ["benchmark", "--model", "-x"],
    ["benchmark", "--scope", "circle"], ["benchmark", "--scope", "pool"], ["benchmark", "--timeout", "300001"], ["benchmark", "--timeout", "0"],
    ["benchmark", "--circle", "Friends"], ["benchmark", "-p", "hello"], ["benchmark", "--verify", "test"], ["benchmark", "--keep=yes"],
  ])("rejects invalid invocation %j", (...args) => {
    expect(() => parseArgs(args)).toThrow();
  });
  it.each([["status", "--runs", "3"], ["models", "list", "--keep"], ["run", "hi", "--restore"], ["setup", "--runs", "3"], ["--keep"]])("keeps benchmark options to benchmark: %j", (...args) => {
    expect(() => parseArgs(args)).toThrow();
  });
  it("leaves the limits and flags of other commands as they were", () => {
    expect(() => parseArgs(["status", "--timeout", "30001"])).toThrow();
    expect(() => parseArgs(["models", "list", "--yes"])).toThrow();
    expect(() => parseArgs(["models", "list", "--model", "x"])).toThrow();
    expect(parseArgs(["status"]).timeout).toBe(3000);
    expect(parseArgs(["run", "hi"]).timeout).toBe(120000);
  });
});
