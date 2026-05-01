/**
 * VaultPanel — point Potluck at a folder of markdown notes and ingest them
 * as 'reference' memories.
 *
 * UI:
 *   - Folder path text input + Save button (persists settings.vault.folder).
 *   - Rescan button (disabled while a scan is running).
 *   - Status card: file count, chunk count, last scan time.
 *   - Disconnect button — wipes all vault content + clears config.
 *
 * Note: a native folder picker would be nicer but requires Electron IPC
 * plumbing. Text input is the v0 path; users can paste an absolute path.
 */

import { useEffect, useState } from 'react';
import { FolderTree, RefreshCw, Trash2, AlertCircle, CheckCircle2 } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi } from '@/lib/localApi';
import type { PotluckSettings, VaultStatus, VaultRescanResult } from '@/lib/localApi';

interface VaultPanelProps {
  settings: PotluckSettings;
  isSaving: boolean;
  saveError: string | null;
  onUpdate: (patch: Partial<PotluckSettings>) => Promise<void>;
}

function formatRelative(iso: string | null): string {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diffSec = Math.max(0, (now - then) / 1000);
  if (diffSec < 60) return 'just now';
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

export function VaultPanel({ settings, isSaving, saveError, onUpdate }: VaultPanelProps) {
  const [pathInput, setPathInput] = useState<string>(settings.vault.folder ?? '');
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [isScanning, setIsScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<VaultRescanResult | null>(null);

  // Sync local input when settings reload (e.g. after Save)
  useEffect(() => {
    setPathInput(settings.vault.folder ?? '');
  }, [settings.vault.folder]);

  // Fetch live status on mount + after every rescan
  useEffect(() => {
    void localApi.vault.status().then(setStatus).catch(() => setStatus(null));
  }, []);

  const dirty = (pathInput.trim() || null) !== (settings.vault.folder ?? null);
  const canScan = !!settings.vault.folder && !isScanning && !dirty;

  const handleSave = async () => {
    const folder = pathInput.trim() || null;
    await onUpdate({ vault: { ...settings.vault, folder } });
  };

  const handleRescan = async () => {
    setIsScanning(true);
    setScanError(null);
    setLastResult(null);
    try {
      const result = await localApi.vault.rescan();
      setLastResult(result);
      const fresh = await localApi.vault.status();
      setStatus(fresh);
    } catch (err) {
      setScanError(err instanceof Error ? err.message : 'Rescan failed');
    } finally {
      setIsScanning(false);
    }
  };

  const handleDisconnect = async () => {
    if (!confirm('Wipe all ingested vault content and forget the folder?')) return;
    setIsScanning(true);
    setScanError(null);
    try {
      await localApi.vault.disconnect();
      const fresh = await localApi.vault.status();
      setStatus(fresh);
      setPathInput('');
      setLastResult(null);
    } catch (err) {
      setScanError(err instanceof Error ? err.message : 'Disconnect failed');
    } finally {
      setIsScanning(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header explainer */}
      <div className="flex items-start gap-3 rounded-xl border border-primary/20 bg-primary/5 px-4 py-3">
        <FolderTree className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <div>
          <p className="text-sm font-medium text-foreground">Ingest a folder of markdown notes.</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Each heading becomes a node in your memory graph. Files stay on disk;
            only embeddings + chunked text are stored locally. Connected agents
            can retrieve from these chunks via the same memory_search MCP tool.
          </p>
        </div>
      </div>

      {saveError && (
        <p className="text-sm text-destructive" role="alert">{saveError}</p>
      )}

      {/* Folder path input */}
      <div className="space-y-2">
        <label htmlFor="vault-folder" className="text-sm font-medium text-foreground">
          Vault folder
        </label>
        <p className="text-xs text-muted-foreground">
          Absolute path. Subfolders are scanned recursively for <code>.md</code> files.
        </p>
        <div className="flex gap-2">
          <input
            id="vault-folder"
            type="text"
            value={pathInput}
            onChange={(e) => setPathInput(e.target.value)}
            placeholder="/Users/you/Documents/notes"
            className={cn(
              'flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm',
              'focus:outline-none focus:ring-2 focus:ring-ring',
            )}
            spellCheck={false}
          />
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={!dirty || isSaving}
            className={cn(
              'rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground',
              'hover:bg-primary/90 transition-colors',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {isSaving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {/* Status card */}
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="grid grid-cols-3 gap-4">
          <Stat label="Files" value={status?.file_count ?? 0} />
          <Stat label="Chunks" value={status?.chunk_count ?? 0} />
          <Stat label="Last scan" value={formatRelative(status?.last_scan_at ?? null)} />
        </div>

        <div className="mt-4 flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleRescan()}
            disabled={!canScan}
            className={cn(
              'flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm',
              'hover:bg-accent transition-colors',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
            aria-label="Rescan vault"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', isScanning && 'animate-spin')} aria-hidden="true" />
            {isScanning ? 'Scanning…' : 'Rescan'}
          </button>

          {settings.vault.folder && (
            <button
              type="button"
              onClick={() => void handleDisconnect()}
              disabled={isScanning}
              className={cn(
                'flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm',
                'text-destructive hover:bg-destructive/10 transition-colors',
                'disabled:cursor-not-allowed disabled:opacity-50',
              )}
              aria-label="Disconnect vault"
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              Disconnect
            </button>
          )}

          {dirty && (
            <p className="text-xs text-muted-foreground">Save the new folder before scanning.</p>
          )}
        </div>

        {scanError && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
            <p className="text-xs text-destructive">{scanError}</p>
          </div>
        )}

        {lastResult && !scanError && (
          <div className="mt-3 flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-2.5">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" aria-hidden="true" />
            <p className="text-xs text-foreground/80">
              Scanned {lastResult.files_scanned} file{lastResult.files_scanned === 1 ? '' : 's'}
              {lastResult.files_added > 0 && ` — ${lastResult.files_added} new`}
              {lastResult.files_updated > 0 && `, ${lastResult.files_updated} updated`}
              {lastResult.files_removed > 0 && `, ${lastResult.files_removed} removed`}
              {' · '}
              {lastResult.chunks_inserted > 0 && `+${lastResult.chunks_inserted}`}
              {lastResult.chunks_inserted > 0 && lastResult.chunks_deleted > 0 && '/'}
              {lastResult.chunks_deleted > 0 && `-${lastResult.chunks_deleted}`}
              {(lastResult.chunks_inserted > 0 || lastResult.chunks_deleted > 0) && ' chunks'}
              {lastResult.chunks_inserted === 0 && lastResult.chunks_deleted === 0 && ' (no changes)'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-lg font-semibold tabular-nums text-foreground">{value}</span>
    </div>
  );
}
