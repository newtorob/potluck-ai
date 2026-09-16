import { describe, expect, it } from 'vitest';
import { checkModelFit, suggestedDownload } from './modelFit';
import type { CatalogModel } from '@/stores/modelStore';
import type { HardwareInfo } from '@/hooks/useHardware';

const hardware: HardwareInfo = { os: 'linux', arch: 'x64', cpu_name: 'CPU', cpu_cores_physical: 8,
  cpu_cores_logical: 16, ram_total_gb: 16, ram_available_gb: 5, gpu_name: 'GPU', gpu_vram_gb: 8, recommended_tier: 'medium' };
const model: CatalogModel = { slug: 'small', name: 'Small', tier: 'small', description: '', size_bytes: 2e9,
  min_ram_gb: 8, min_vram_gb: 4, context_window: 8192, recommended_tier: 'small', is_featured: false, installed: false };

describe('model choice', () => {
  it('does not recommend a featured model that exceeds GPU or total memory', () => {
    expect(suggestedDownload([model, { ...model, slug: 'large', is_featured: true, min_vram_gb: 24 }], hardware)).toBe('small');
    expect(suggestedDownload([{ ...model, min_ram_gb: 32 }], hardware)).toBeNull();
  });

  it('never asks the member to download an already-installed recommendation', () => {
    expect(suggestedDownload([{ ...model, installed: true, is_featured: true }], hardware)).toBeNull();
  });

  it('does not claim compatibility before hardware detection; supports CPU-only capacity', () => {
    expect(checkModelFit(model, null)).toBe('unknown');
    expect(suggestedDownload([model], null)).toBeNull();
    expect(checkModelFit(model, { ...hardware, gpu_name: null, gpu_vram_gb: null })).toBe('ok');
  });
});
