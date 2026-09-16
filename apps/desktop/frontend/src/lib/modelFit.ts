import type { CatalogModel } from '@/stores/modelStore';
import type { HardwareInfo } from '@/hooks/useHardware';

export type ModelFit = 'ok' | 'borderline' | 'insufficient' | 'unknown';

/** Catalog requirements are a capacity estimate, not a guarantee about free
 * memory or inference speed. CPU-only machines may run without GPU memory. */
export function checkModelFit(model: CatalogModel, hardware: HardwareInfo | null): ModelFit {
  if (!hardware) return 'unknown';
  const ram = hardware.ram_total_gb / Math.max(model.min_ram_gb, 1);
  const vram = hardware.gpu_vram_gb === null || model.min_vram_gb === 0
    ? 1 : hardware.gpu_vram_gb / model.min_vram_gb;
  if (ram >= 1 && vram >= 1) return 'ok';
  if (ram >= 0.8 && vram >= 0.8) return 'borderline';
  return 'insufficient';
}

export function suggestedDownload(models: CatalogModel[], hardware: HardwareInfo | null): string | null {
  if (!hardware) return null;
  const candidates = models.filter((model) => !model.installed && checkModelFit(model, hardware) === 'ok');
  candidates.sort((a, b) =>
    Number(b.is_featured) - Number(a.is_featured)
    || Number(b.tier === hardware.recommended_tier) - Number(a.tier === hardware.recommended_tier)
    || a.size_bytes - b.size_bytes,
  );
  return candidates[0]?.slug ?? null;
}
