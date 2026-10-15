import { describe, expect, it } from 'vitest';
import { parseModelRecommendations } from './modelRecommendations';
const model = (id: string, extra = {}) => ({ id, name: id, sizeBytes: 100, fits: true, ...extra });
const data = (models: unknown[], ids: string[]) => ({
  schemaVersion: 1,
  useCase: 'coding',
  models,
  recommended: ids.map((id) => ({ id, tokensPerSecond: 25 })),
});
describe('recommendation contract', () => {
  it('preserves order, removes duplicates, failed and unknown fits, and caps at three', () => {
    const out = parseModelRecommendations(
      data(
        [
          model('failed', { checked: 'failed' }),
          model('unknown', { fits: null }),
          ...['a', 'b', 'c', 'd'].map((id) => model(id)),
        ],
        ['missing', 'failed', 'unknown', 'b', 'b', 'a', 'd', 'c'],
      ),
      'coding',
    );
    expect(out.map((m) => m.id)).toEqual(['b', 'a', 'd']);
  });
  it('does not present unproven speed or context as measured', () => {
    const out = parseModelRecommendations(
      data([model('a', { measured: { contextTokens: 4096 } })], ['a']),
      'coding',
    );
    expect(out[0]).toMatchObject({ checked: false, tokensPerSecond: null, contextTokens: null });
  });
  it('shows valid local evidence and rejects nonfinite numbers', () => {
    const out = parseModelRecommendations(
      data(
        [
          model('a', {
            checked: 'passed',
            measured: { contextTokens: 4096 },
            useCases: ['coding'],
          }),
          model('b', { checked: 'passed', measured: { contextTokens: Infinity } }),
        ],
        ['a', 'b'],
      ),
      'coding',
    );
    expect(out[0]).toMatchObject({
      checked: true,
      tokensPerSecond: 25,
      contextTokens: 4096,
      taskMatch: true,
    });
    expect(out[1]?.contextTokens).toBeNull();
  });
  it('requires the requested intended use to be acknowledged by the engine', () => {
    expect(() => parseModelRecommendations(data([], []), 'writing')).toThrow(/Update/);
    expect(() =>
      parseModelRecommendations({ ...data([], []), recommended: null }, 'coding'),
    ).toThrow(/Update/);
  });
});
