export const MODEL_USES = ['general', 'coding', 'writing', 'reasoning'] as const;
export type ModelUse = (typeof MODEL_USES)[number];
export const MODEL_USE_LABELS: Record<ModelUse, string> = {
  general: 'General',
  coding: 'Coding',
  writing: 'Writing',
  reasoning: 'Analysis',
};

export interface ModelSuggestion {
  id: string;
  name: string;
  sizeBytes: number;
  installed: boolean;
  checked: boolean;
  reason: string | null;
  taskMatch: boolean;
  contextTokens: number | null;
  tokensPerSecond: number | null;
}

const record = (v: unknown): Record<string, unknown> | null =>
  typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
const positive = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null;

/** Preserve engine order. Never invent a fallback when resources or checks rule it out. */
export function parseModelRecommendations(value: unknown, use: ModelUse): ModelSuggestion[] {
  const data = record(value);
  if (
    data?.['schemaVersion'] !== 1 ||
    data['useCase'] !== use ||
    !Array.isArray(data['recommended']) ||
    !Array.isArray(data['models'])
  ) {
    throw new Error(
      'Update the Potluck engine to get model recommendations for your intended use.',
    );
  }
  const candidates = data['models'].map(record);
  const seen = new Set<string>();
  const result: ModelSuggestion[] = [];
  for (const raw of data['recommended']) {
    const pick = record(raw);
    const id = pick?.['id'];
    if (typeof id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}$/.test(id) || seen.has(id))
      continue;
    const model = candidates.find((m) => m?.['id'] === id);
    const size = positive(model?.['sizeBytes']);
    if (!model || !size || model['fits'] !== true || model['checked'] === 'failed') continue;
    seen.add(id);
    const checked = model['checked'] === 'passed';
    const measured = record(model['measured']);
    result.push({
      id,
      name: typeof model['name'] === 'string' ? model['name'] : id,
      sizeBytes: size,
      installed: model['installed'] === true,
      checked,
      reason:
        typeof model['recommendationReason'] === 'string' ? model['recommendationReason'] : null,
      taskMatch: Array.isArray(model['useCases']) && model['useCases'].includes(use),
      contextTokens: checked ? positive(measured?.['contextTokens']) : null,
      tokensPerSecond: checked ? positive(pick?.['tokensPerSecond']) : null,
    });
    if (result.length === 3) break;
  }
  return result;
}
