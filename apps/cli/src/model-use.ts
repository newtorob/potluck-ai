import type { OnboardingIO } from "./operational-setup.js";
import { RuntimeError } from "./runtime.js";

export const MODEL_USES = ["general", "coding", "writing", "reasoning"] as const;
export type ModelUse = typeof MODEL_USES[number];
export const isModelUse = (value: unknown): value is ModelUse => MODEL_USES.includes(value as ModelUse);
export const useLabel: Record<ModelUse, string> = {
  general: "General use", coding: "Coding", writing: "Writing and brainstorming", reasoning: "Analysis and reasoning",
};

export async function askModelUse(io: OnboardingIO): Promise<ModelUse> {
  io.progress("What will you mainly use Potluck for?\n  1. Coding\n  2. Writing and brainstorming\n  3. Analysis and reasoning\n  4. General use / not sure\n");
  while (true) {
    const answer = await io.ask("Use [4], or skip: ");
    if (answer === null || io.signal?.aborted) throw new RuntimeError("Setup interrupted. Run setup again to resume.");
    const value = answer.trim().toLowerCase();
    const numbered: Record<string, ModelUse> = { "1": "coding", "2": "writing", "3": "reasoning", "4": "general", "": "general", skip: "general" };
    if (Object.hasOwn(numbered, value)) return numbered[value];
    if (isModelUse(value)) return value;
    io.progress("Choose 1-4, coding, writing, reasoning, general, or skip.\n");
  }
}
