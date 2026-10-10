/**
 * ModelSelector — dropdown of installed models.
 *
 * Fetches model list directly from the sidecar (does NOT touch modelStore).
 * Polls every 30 seconds and refetches on window focus.
 *
 * Clicking an unloaded model triggers POST /models/{slug}/load with a spinner.
 * Shows a green dot when a model is loaded, grey otherwise. A quiet "On CPU"
 * follows the name when the engine's answer check moved the model off the GPU.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, Circle, Loader2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import { movedToCpu } from '@/lib/acceptance';
import { useAcceptanceStore } from '@/stores/acceptanceStore';
import type { Model } from '@/types/models';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ModelSelectorProps {
  /** Disable the selector while a chat is streaming */
  disabled?: boolean;
}

export function ModelSelector({ disabled = false }: ModelSelectorProps) {
  const navigate = useNavigate();
  const [models, setModels] = useState<Model[]>([]);
  const [loadedSlug, setLoadedSlug] = useState<string | null>(null);
  const [isLoadingModel, setIsLoadingModel] = useState(false);
  const [isOpen, setIsOpen] = useState(false);
  const [fetchError, setFetchError] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const acceptance = useAcceptanceStore((s) => s.acceptance);
  const onCpu = movedToCpu(acceptance, loadedSlug);

  const fetchModels = useCallback(async () => {
    try {
      const [all, health] = await Promise.all([
        localApi.listModels(),
        localApi.health(),
      ]);
      const installed = all.filter((m) => m.installed);
      setModels(installed);
      setLoadedSlug(health.active_model_id);
      setFetchError(false);
    } catch {
      setFetchError(true);
    }
  }, []);

  // Initial fetch + 30-second poll
  useEffect(() => {
    void fetchModels();
    const interval = setInterval(() => void fetchModels(), 30_000);
    return () => clearInterval(interval);
  }, [fetchModels]);

  // Refetch on window focus
  useEffect(() => {
    const onFocus = () => void fetchModels();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [fetchModels]);

  // Close dropdown on outside click
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [isOpen]);

  // Close on Escape
  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [isOpen]);

  const activeModel = models.find((m) => m.slug === loadedSlug) ?? null;

  async function handleSelectModel(slug: string) {
    setIsOpen(false);
    if (slug === loadedSlug) return;
    setIsLoadingModel(true);
    try {
      await localApi.loadModel(slug);
      setLoadedSlug(slug);
      void useAcceptanceStore.getState().refresh();
    } catch {
      // silently fall through; model stays at prior state
    } finally {
      setIsLoadingModel(false);
    }
  }

  const buttonLabel = isLoadingModel
    ? 'Loading model…'
    : activeModel?.name ?? (fetchError ? 'Error loading models' : 'No model loaded');

  return (
    <div ref={dropdownRef} className="relative">
      <button
        type="button"
        onClick={() => !disabled && !isLoadingModel && setIsOpen((v) => !v)}
        disabled={disabled || isLoadingModel}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label="Select model"
        className={cn(
          'flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5',
          'text-sm font-medium text-foreground transition-colors',
          'hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          (disabled || isLoadingModel) && 'cursor-not-allowed opacity-50',
        )}
      >
        {isLoadingModel ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-hidden="true" />
        ) : (
          <Circle
            className={cn(
              'h-2.5 w-2.5 fill-current',
              loadedSlug ? 'text-sage' : 'text-muted-foreground',
            )}
            aria-hidden="true"
          />
        )}
        <span className="max-w-[160px] truncate">{buttonLabel}</span>
        {onCpu && !isLoadingModel && (
          <span
            className="shrink-0 text-xs font-normal text-muted-foreground"
            {...(acceptance.message !== null ? { title: acceptance.message } : {})}
          >
            On CPU
          </span>
        )}
        <ChevronDown
          className={cn('h-3.5 w-3.5 text-muted-foreground transition-transform', isOpen && 'rotate-180')}
          aria-hidden="true"
        />
      </button>

      {isOpen && (
        <div
          role="listbox"
          aria-label="Available models"
          className={cn(
            'absolute left-0 top-full z-50 mt-1 min-w-[220px] rounded-lg border border-border',
            'bg-popover shadow-lg',
          )}
        >
          {models.length === 0 ? (
            <div className="px-3 py-3 text-sm text-muted-foreground">
              No installed models.{' '}
              <button
                type="button"
                onClick={() => { setIsOpen(false); navigate('/models'); }}
                className="text-primary underline-offset-2 hover:underline"
              >
                Browse models
              </button>
            </div>
          ) : (
            <ul className="max-h-64 overflow-y-auto py-1">
              {models.map((model) => {
                const isActive = model.slug === loadedSlug;
                return (
                  <li key={model.slug}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={isActive}
                      onClick={() => void handleSelectModel(model.slug)}
                      className={cn(
                        'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors',
                        'hover:bg-accent hover:text-accent-foreground',
                        isActive && 'bg-primary/10 text-primary',
                      )}
                    >
                      <Circle
                        className={cn(
                          'h-2 w-2 shrink-0 fill-current',
                          isActive ? 'text-sage' : 'text-transparent',
                        )}
                        aria-hidden="true"
                      />
                      <span className="flex-1 truncate">{model.name}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {model.parameters}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="border-t border-border px-3 py-2">
            <button
              type="button"
              onClick={() => { setIsOpen(false); navigate('/models'); }}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors"
            >
              Manage models →
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

