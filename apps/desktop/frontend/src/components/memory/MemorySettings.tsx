/**
 * MemorySettings — embeddable panel for Settings → Privacy tab.
 *
 * Rendered by PrivacyPanel (Agent B integrates it). Accepts settings props
 * passed down from SettingsView so it doesn't need its own settings fetch.
 *
 * Contains:
 *   - Master memory toggle (privacy.memory_enabled)
 *   - Subtitle describing local-only storage
 *   - Export, Import, Wipe actions
 *   - Memory count display
 */

import { useRef, useState } from 'react';
import { Download, Upload, Trash2, Check } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { useMemoryStore } from '@/stores/memoryStore';
import type { PotluckSettings } from '@/lib/localApi';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface MemorySettingsProps {
  settings: PotluckSettings;
  isSaving: boolean;
  onUpdate: (patch: Partial<PotluckSettings>) => Promise<void>;
}

// ---------------------------------------------------------------------------
// WipeConfirmDialog — modal dialog requiring typed confirmation
// ---------------------------------------------------------------------------

interface WipeConfirmDialogProps {
  onConfirm: () => void;
  onCancel: () => void;
  isWiping: boolean;
  error: string | null;
}

function WipeConfirmDialog({ onConfirm, onCancel, isWiping, error }: WipeConfirmDialogProps) {
  const [checked, setChecked] = useState(false);

  const handleBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) onCancel();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onCancel();
  };

  return (
    // Backdrop
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wipe-dialog-title"
      onClick={handleBackdropClick}
      onKeyDown={handleKeyDown}
    >
      <div className="w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-xl">
        <h2 id="wipe-dialog-title" className="mb-2 text-sm font-semibold text-foreground">
          Wipe all memory?
        </h2>
        <p className="mb-4 text-xs text-muted-foreground">
          This deletes every memory permanently. There is no undo. Export your memories first if
          you want a backup.
        </p>

        <label className="mb-4 flex cursor-pointer items-start gap-2">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            className="mt-0.5 accent-destructive"
            aria-label="I understand this is permanent"
          />
          <span className="text-xs text-muted-foreground">I understand this cannot be undone</span>
        </label>

        {error && (
          <p className="mb-3 text-xs text-destructive" role="alert">
            {error}
          </p>
        )}

        <div className="flex gap-2">
          <button
            type="button"
            onClick={onConfirm}
            disabled={!checked || isWiping}
            className={cn(
              'flex-1 rounded-lg bg-destructive px-3 py-2 text-xs font-medium text-destructive-foreground',
              'hover:bg-destructive/90 disabled:cursor-not-allowed disabled:opacity-50 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            {isWiping ? 'Wiping…' : 'Wipe all memory'}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={isWiping}
            className={cn(
              'rounded-lg px-3 py-2 text-xs font-medium text-muted-foreground',
              'hover:bg-muted hover:text-foreground transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// InlineToast — lightweight success message (no external dep)
// ---------------------------------------------------------------------------

interface InlineToastProps {
  message: string;
  className?: string;
}

function InlineToast({ message, className }: InlineToastProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5',
        'text-xs font-medium text-emerald-400',
        className,
      )}
      role="status"
      aria-live="polite"
    >
      <Check className="h-3 w-3" aria-hidden="true" />
      {message}
    </span>
  );
}

// ---------------------------------------------------------------------------
// MemorySettings
// ---------------------------------------------------------------------------

export function MemorySettings({ settings, isSaving, onUpdate }: MemorySettingsProps) {
  const { memories, exportToFile, importFromFile, wipeAll, wipeError } = useMemoryStore();

  // Privacy setting — default true if the field doesn't exist yet on the
  // settings shape (forward-compat: Agent B adds memory_enabled to the schema)
  const memoryEnabled =
    'memory_enabled' in (settings.privacy as Record<string, unknown>)
      ? Boolean((settings.privacy as Record<string, unknown>)['memory_enabled'])
      : true;

  const [exportSuccess, setExportSuccess] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [wipeOpen, setWipeOpen] = useState(false);
  const [isWiping, setIsWiping] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
  const [isImporting, setIsImporting] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleToggleMemory = () => {
    void onUpdate({
      privacy: {
        ...settings.privacy,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- extending settings shape forward-compatibly
        memory_enabled: !memoryEnabled,
      } as any,
    });
  };

  const handleExport = async () => {
    setIsExporting(true);
    setExportSuccess(false);
    try {
      await exportToFile();
      setExportSuccess(true);
      setTimeout(() => setExportSuccess(false), 3000);
    } finally {
      setIsExporting(false);
    }
  };

  const handleImportClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportError(null);
    setImportMsg(null);
    setIsImporting(true);
    try {
      const result = await importFromFile(file);
      setImportMsg(`Imported ${result.imported} · Skipped ${result.skipped}`);
      setTimeout(() => setImportMsg(null), 4000);
    } catch (err) {
      setImportError(err instanceof Error ? err.message : 'Import failed');
    } finally {
      setIsImporting(false);
      // Reset file input so the same file can be re-imported
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleWipeConfirm = async () => {
    setIsWiping(true);
    try {
      await wipeAll();
      setWipeOpen(false);
    } finally {
      setIsWiping(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Section heading */}
      <div className="border-t border-border pt-4">
        <h3 className="mb-0.5 text-sm font-medium text-foreground">Memory</h3>
      </div>

      {/* Master toggle */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <label
            htmlFor="memory-enabled-toggle"
            className="cursor-pointer text-sm font-medium text-foreground"
          >
            Memory enabled
          </label>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Potluck remembers facts, preferences, and project context across your conversations.
            Memory lives entirely on your device — see Privacy.
          </p>
        </div>

        <button
          id="memory-enabled-toggle"
          role="switch"
          type="button"
          aria-checked={memoryEnabled}
          disabled={isSaving}
          onClick={handleToggleMemory}
          className={cn(
            'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full',
            'transition-colors duration-200',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-50',
            memoryEnabled ? 'bg-primary' : 'bg-muted',
          )}
          aria-label="Toggle memory"
        >
          <span
            className={cn(
              'pointer-events-none inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm',
              'transition-transform duration-200',
              memoryEnabled ? 'translate-x-[18px]' : 'translate-x-[3px]',
            )}
          />
        </button>
      </div>

      {/* Memory count */}
      <p className="text-xs text-muted-foreground">
        You have <span className="font-medium text-foreground">{memories.length}</span>{' '}
        {memories.length === 1 ? 'memory' : 'memories'} stored.
      </p>

      {/* Action buttons */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Export */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleExport()}
            disabled={isExporting}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5',
              'text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground',
              'disabled:cursor-not-allowed disabled:opacity-50 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Download className="h-3 w-3" aria-hidden="true" />
            {isExporting ? 'Exporting…' : 'Export memory'}
          </button>
          {exportSuccess && <InlineToast message="Downloaded" />}
        </div>

        {/* Import */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleImportClick}
            disabled={isImporting}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5',
              'text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground',
              'disabled:cursor-not-allowed disabled:opacity-50 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Upload className="h-3 w-3" aria-hidden="true" />
            {isImporting ? 'Importing…' : 'Import memory'}
          </button>
          {importMsg && <InlineToast message={importMsg} />}
          {importError && (
            <span className="text-xs text-destructive" role="alert">
              {importError}
            </span>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".json"
            onChange={(e) => void handleFileChange(e)}
            className="sr-only"
            aria-label="Import memory file"
            tabIndex={-1}
          />
        </div>

        {/* Wipe */}
        <button
          type="button"
          onClick={() => setWipeOpen(true)}
          className={cn(
            'flex items-center gap-1.5 rounded-lg border border-destructive/40 px-3 py-1.5',
            'text-xs font-medium text-destructive hover:bg-destructive/10',
            'transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <Trash2 className="h-3 w-3" aria-hidden="true" />
          Wipe all memory
        </button>
      </div>

      {wipeOpen && (
        <WipeConfirmDialog
          onConfirm={() => void handleWipeConfirm()}
          onCancel={() => setWipeOpen(false)}
          isWiping={isWiping}
          error={wipeError}
        />
      )}
    </div>
  );
}
