/**
 * AnswerCheckBanner: a thin top strip shown when the engine's answer check
 * moved the loaded model from the GPU to the CPU, or when the model failed
 * the check.
 *
 * The strip says what the engine said (`acceptance.message`) and nothing the
 * app made up. When the model was moved to the CPU, a smaller second line names
 * the model and says it is on the CPU. Passed, running, skipped, not run and an
 * engine without the check all show nothing.
 *
 * It reads the check when the engine connects and when the loaded model
 * changes (the model picker and model store also ask right after a load). The
 * X closes it for this session; it returns if the model or the verdict changes.
 */
import { useEffect } from 'react';
import { X } from 'lucide-react';
import { useHealth } from '@/hooks/useHealth';
import { needsBanner, bannerKey } from '@/lib/acceptance';
import { formatModelName } from '@/lib/modelName';
import { useAcceptanceStore } from '@/stores/acceptanceStore';
import { useModelStore } from '@/stores/modelStore';

export function AnswerCheckBanner() {
  const { sidecarUp, activeModelId } = useHealth();
  const acceptance = useAcceptanceStore((s) => s.acceptance);
  const dismissedKey = useAcceptanceStore((s) => s.dismissedKey);
  const refresh = useAcceptanceStore((s) => s.refresh);
  const dismiss = useAcceptanceStore((s) => s.dismiss);
  const models = useModelStore((s) => s.models);

  useEffect(() => {
    if (sidecarUp) void refresh();
  }, [sidecarUp, activeModelId, refresh]);

  if (!needsBanner(acceptance) || dismissedKey === bannerKey(acceptance)) return null;

  const slug = acceptance.model_id;
  const name =
    slug === null
      ? 'This model'
      : (models.find((m) => m.slug === slug)?.name ?? formatModelName(slug));

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex shrink-0 items-start justify-center gap-3 border-b border-amber-500/30 bg-amber-500/10 px-4 py-1.5 text-[12px] text-amber-700 dark:text-amber-300"
    >
      <div className="min-w-0 text-center">
        <p>{acceptance.message}</p>
        {acceptance.fallback !== null && (
          <p className="text-[11px] opacity-80">{name} is running on the CPU.</p>
        )}
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss"
        className="mt-0.5 shrink-0 text-amber-700/70 hover:text-amber-700 dark:text-amber-300/70 dark:hover:text-amber-300"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
