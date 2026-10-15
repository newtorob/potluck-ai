import { describe, expect, it } from "vitest";
import { AiderContextError, planAiderContext } from "../src/aider-context.js";

describe("Aider launch context policy", () => {
  it.each([undefined, NaN, Infinity, -Infinity, 0, -1, 8192.5, Number.MAX_SAFE_INTEGER + 1])(
    "refuses unknown or invalid loaded context %s", window => {
      expect(() => planAiderContext(window)).toThrow(AiderContextError);
      expect(() => planAiderContext(window)).toThrow("Update Potluck");
    },
  );

  it.each([1, 4096, 8191])("refuses a loaded %i-token window with a concrete remedy", window => {
    expect(() => planAiderContext(window)).toThrow(`this model has ${window}`);
    expect(() => planAiderContext(window)).toThrow("Reload it with a larger context");
  });

  it.each([
    [8192, 2048, 5632, 1408, 704],
    [8193, 2048, 5633, 1408, 704],
    [16384, 4096, 11776, 2944, 1024],
    [32768, 4096, 28160, 4096, 1024],
    [131072, 4096, 126464, 4096, 1024],
  ])("reserves output and template headroom within a loaded %i-token window", (window, output, input, history, map) => {
    const budget = planAiderContext(window);
    expect(budget).toEqual({ contextWindowTokens: window, maxOutputTokens: output,
      maxInputTokens: input, maxHistoryTokens: history, mapTokens: map });
    expect(budget.maxInputTokens + budget.maxOutputTokens + 512).toBe(window);
    expect(budget.maxHistoryTokens + budget.mapTokens).toBeLessThan(budget.maxInputTokens);
  });
});
