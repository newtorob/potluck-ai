import { describe, expect, it } from "vitest";

import { parseRelaxedJson } from "../src/relaxedJson.js";

describe("parseRelaxedJson", () => {
  it("parses single-quoted values that contain unescaped double quotes", () => {
    // The exact shape weaker models emit: single-quoted string VALUES whose
    // contents include embedded double quotes (a naive ' -> " swap would break).
    const input =
      `{"tool": "edit_file", "args": {"path": "src/index.ts", "old_string": '    if (line === "/exit" || line === "/quit") break;', "new_string": '    if (line === "/exit" || line === "/quit" || line === "/tools") break;'}}`;
    expect(parseRelaxedJson(input)).toEqual({
      tool: "edit_file",
      args: {
        path: "src/index.ts",
        old_string: '    if (line === "/exit" || line === "/quit") break;',
        new_string:
          '    if (line === "/exit" || line === "/quit" || line === "/tools") break;',
      },
    });
  });

  it("parses single-quoted keys and values and mixes both quote styles", () => {
    expect(parseRelaxedJson(`{'a': 'x', "b": 'y', 'c': "z"}`)).toEqual({
      a: "x",
      b: "y",
      c: "z",
    });
  });

  it("allows unescaped opposite quotes inside a string", () => {
    expect(parseRelaxedJson(`{'a': "it's fine"}`)).toEqual({ a: "it's fine" });
    expect(parseRelaxedJson(`{"a": 'say "hi"'}`)).toEqual({ a: 'say "hi"' });
  });

  it("tolerates trailing commas in objects and arrays", () => {
    expect(parseRelaxedJson('{"a": 1, "b": 2,}')).toEqual({ a: 1, b: 2 });
    expect(parseRelaxedJson("[1, 2, 3,]")).toEqual([1, 2, 3]);
    expect(parseRelaxedJson('{"a": [1, 2,], "b": {"c": 3,},}')).toEqual({
      a: [1, 2],
      b: { c: 3 },
    });
  });

  it("accepts unquoted object keys", () => {
    expect(parseRelaxedJson("{tool: 'read_file', args: {path: 'a.ts'}}")).toEqual({
      tool: "read_file",
      args: { path: "a.ts" },
    });
    expect(parseRelaxedJson("{$x: 1, _y: 2, a1: 3}")).toEqual({ $x: 1, _y: 2, a1: 3 });
  });

  it("parses deeply nested structures and arrays", () => {
    const input = `{a: [1, {b: [2, {c: 'deep'}]}, [[3]]]}`;
    expect(parseRelaxedJson(input)).toEqual({
      a: [1, { b: [2, { c: "deep" }] }, [[3]]],
    });
  });

  it("parses numbers, booleans, and null", () => {
    expect(parseRelaxedJson("[-1, 2.5, 3e2, -4.2e-3, 0, true, false, null]")).toEqual([
      -1, 2.5, 300, -0.0042, 0, true, false, null,
    ]);
  });

  it("handles escapes inside both quote styles", () => {
    // Raw input chars: a \n b \t c \" d \' e \\ f A
    const single = String.raw`{"s": 'a\nb\tc\"d\'e\\fA'}`;
    const dbl = String.raw`{"s": "a\nb\tc\"d\'e\\fA"}`;
    const expected = { s: "a\nb\tc\"d'e\\fA" };
    expect(parseRelaxedJson(single)).toEqual(expected);
    expect(parseRelaxedJson(dbl)).toEqual(expected);
  });

  it("returns the same structure as JSON.parse for valid strict JSON", () => {
    const cases = [
      '{"a":1,"b":[true,false,null],"c":{"d":-2.5e3}}',
      "[]",
      "{}",
      '"hello"',
      "42",
      "true",
      "null",
      '{"nested":{"x":[1,2,{"y":"z"}]}}',
      '["a\\nb","c\\td","\\u00e9"]',
    ];
    for (const c of cases) {
      expect(parseRelaxedJson(c)).toEqual(JSON.parse(c));
    }
  });

  it("throws on broken input rather than hanging", () => {
    expect(() => parseRelaxedJson('{"tool":')).toThrow();
    expect(() => parseRelaxedJson("{a: }")).toThrow();
    expect(() => parseRelaxedJson("'unterminated")).toThrow();
    expect(() => parseRelaxedJson("{} garbage")).toThrow();
    expect(() => parseRelaxedJson('{"a": 1} extra')).toThrow();
    expect(() => parseRelaxedJson("")).toThrow();
    expect(() => parseRelaxedJson("[1, 2")).toThrow();
    expect(() => parseRelaxedJson('{"a" 1}')).toThrow();
  });
});
