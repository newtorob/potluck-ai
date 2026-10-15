/** Launch policy for Aider, using the selected runtime's loaded context. */
export interface AiderContextBudget {
  contextWindowTokens: number;
  maxOutputTokens: number;
  maxInputTokens: number;
  maxHistoryTokens: number;
  mapTokens: number;
}

export class AiderContextError extends Error {}

export function planAiderContext(window: number | undefined): AiderContextBudget {
  if (window === undefined || !Number.isSafeInteger(window) || window <= 0) {
    throw new AiderContextError("Cannot determine the selected model's loaded context. Update Potluck on the local and serving machines, load the model, then retry.");
  }
  // This is Potluck's launch floor, not a claim about every Aider workflow.
  if (window < 8192) {
    throw new AiderContextError(`Potluck requires at least 8192 loaded context tokens to launch Aider; this model has ${window}. Reload it with a larger context or choose another model.`);
  }
  const maxOutputTokens = Math.min(4096, Math.floor(window / 4));
  const maxInputTokens = window - maxOutputTokens - 512;
  return {
    contextWindowTokens: window,
    maxOutputTokens,
    maxInputTokens,
    maxHistoryTokens: Math.min(4096, Math.floor(maxInputTokens / 4)),
    mapTokens: Math.min(1024, Math.floor(maxInputTokens / 8)),
  };
}
