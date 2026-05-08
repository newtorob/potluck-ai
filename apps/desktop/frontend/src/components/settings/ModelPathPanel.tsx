/**
 * ModelPathPanel — shows the model storage directory.
 *
 * "Reveal in Finder" uses the Electron context bridge if available.
 * Falls back to a copy-to-clipboard button when the preload doesn't expose it.
 */

import { useState } from 'react';
import { FolderOpen, Copy, Check } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import type { PotluckSettings } from '@/lib/localApi';

// Electron preload might expose window.potluck
declare global {
  interface Window {
    potluck?: {
      openPath?: (path: string) => void;
    };
  }
}

interface ModelPathPanelProps {
  settings: PotluckSettings;
}

export function ModelPathPanel({ settings }: ModelPathPanelProps) {
  const [copied, setCopied] = useState(false);

  const path = settings.models_path;
  const canReveal = typeof window.potluck?.openPath === 'function';

  const handleReveal = () => {
    if (window.potluck?.openPath) {
      window.potluck.openPath(path);
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable in some contexts — silent fail
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <p className="text-sm font-medium text-foreground mb-1">Model storage path</p>
        <p className="text-xs text-muted-foreground mb-3">
          All downloaded model files (.gguf) are stored here. This path is configured in{' '}
          <code className="font-mono text-xs text-foreground">~/.potluck/config.json</code> and
          cannot be changed through the UI in this release.
        </p>
      </div>

      <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2.5">
        <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
        <code className="flex-1 min-w-0 truncate text-xs font-mono text-foreground">
          {path}
        </code>
      </div>

      <div className="flex gap-2">
        {canReveal ? (
          <button
            type="button"
            onClick={handleReveal}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium',
              'text-muted-foreground hover:text-foreground hover:bg-accent transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            )}
          >
            <FolderOpen className="h-3.5 w-3.5" />
            Reveal in Finder
          </button>
        ) : (
          <button
            type="button"
            onClick={() => void handleCopy()}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium',
              'text-muted-foreground hover:text-foreground hover:bg-accent transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            )}
            aria-label={copied ? 'Path copied' : 'Copy path to clipboard'}
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 text-sage" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" />
                Copy path
              </>
            )}
          </button>
        )}
      </div>
    </div>
  );
}
