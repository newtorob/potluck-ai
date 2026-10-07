/**
 * Which model a trusted-circle request should ask for.
 *
 * A circle request names a model, and only machines offering exactly that
 * model can answer. The coordinator reports what the chosen circle can run
 * right now; the request should use one of those, keeping the current choice
 * whenever the circle still offers it.
 */

import type { CircleModel } from './localApi';

/** The current model if the circle offers it, else the one most machines offer, else none. */
export function pickCircleModel(current: string, models: readonly CircleModel[]): string | null {
  if (models.some((m) => m.slug === current)) return current;
  const first = models[0];
  return first ? first.slug : null;
}

/** "qwen3-4b-q4 (2 machines)" — the count only when it adds something. */
export function circleModelLabel(model: CircleModel): string {
  return model.machines > 1 ? `${model.slug} (${model.machines} machines)` : model.slug;
}
