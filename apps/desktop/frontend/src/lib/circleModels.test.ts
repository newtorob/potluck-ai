import { describe, expect, it } from 'vitest';
import { circleModelLabel, pickCircleModel } from './circleModels';

describe('circle model choice', () => {
  const models = [
    { slug: 'big-model', machines: 2 },
    { slug: 'small-model', machines: 1 },
  ];

  it('keeps the current model while the circle still offers it', () => {
    expect(pickCircleModel('small-model', models)).toBe('small-model');
  });

  it('moves to a model the circle has when the current one is not offered', () => {
    expect(pickCircleModel('qwen2.5:3b-instruct-q4_K_M', models)).toBe('big-model');
  });

  it('has nothing to pick when no circle machine is sharing', () => {
    expect(pickCircleModel('small-model', [])).toBeNull();
  });

  it('mentions the machine count only when there is more than one', () => {
    expect(circleModelLabel(models[0]!)).toBe('big-model (2 machines)');
    expect(circleModelLabel(models[1]!)).toBe('small-model');
  });
});
