import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  Copy,
  Eye,
  FolderOpen,
  Loader2,
  Plug,
  RefreshCw,
  ShieldCheck,
  Unplug,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ipc } from '@/lib/ipc';
import { localApi } from '@/lib/localApi';
import type {
  AgentConnectorState,
  AgentConnectorStatus,
  AgentProvider,
} from '@/types/agents';

const PROJECT_STORAGE_KEY = 'potluck:agent-hook-project';

function savedProjectPath(): string | null {
  try {
    return localStorage.getItem(PROJECT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function providerLabel(provider: AgentProvider): string {
  return provider === 'claude' ? 'Claude Code' : 'Codex';
}

const STATE_STYLE: Record<AgentConnectorState, string> = {
  installed: 'border-sage/30 bg-sage/10 text-sage-foreground',
  partial: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  invalid: 'border-destructive/30 bg-destructive/10 text-destructive',
  unavailable: 'border-destructive/30 bg-destructive/10 text-destructive',
  'not-installed': 'border-border bg-muted text-muted-foreground',
};

const STATE_LABEL: Record<AgentConnectorState, string> = {
  installed: 'Installed',
  partial: 'Needs repair',
  invalid: 'Config error',
  unavailable: 'Bridge missing',
  'not-installed': 'Not installed',
};

function StatusPill({ state }: { state: AgentConnectorState }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-medium',
        STATE_STYLE[state],
      )}
    >
      {state === 'installed' && <CheckCircle2 className="h-3 w-3" aria-hidden="true" />}
      {STATE_LABEL[state]}
    </span>
  );
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1_200);
    } catch {
      // Clipboard access is optional; the snippet remains selectable.
    }
  };
  return (
    <button
      type="button"
      onClick={() => void copy()}
      className={cn(
        'flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium',
        'text-muted-foreground transition-colors hover:bg-accent hover:text-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
      aria-label={copied ? 'Hook configuration copied' : 'Copy hook configuration'}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-sage" aria-hidden="true" />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden="true" />
      )}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

export function AgentHooksPanel() {
  const [provider, setProvider] = useState<AgentProvider>('codex');
  const [projectPath, setProjectPath] = useState<string | null>(savedProjectPath);
  const [status, setStatus] = useState<AgentConnectorStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [action, setAction] = useState<'connect' | 'disconnect' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);

  const loadStatus = useCallback(async (nextProvider: AgentProvider, path: string) => {
    const request = ++requestRef.current;
    setIsLoading(true);
    setError(null);
    try {
      const next = await localApi.agents.connectorStatus(nextProvider, path);
      if (request === requestRef.current) setStatus(next);
    } catch (reason) {
      if (request === requestRef.current) {
        setStatus(null);
        setError(reason instanceof Error ? reason.message : 'Could not inspect the provider config.');
      }
    } finally {
      if (request === requestRef.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (projectPath === null) {
      setStatus(null);
      return;
    }
    void loadStatus(provider, projectPath);
  }, [loadStatus, projectPath, provider]);

  const chooseProject = async () => {
    const selected = await ipc.selectDirectory();
    if (selected === null) return;
    try {
      localStorage.setItem(PROJECT_STORAGE_KEY, selected);
    } catch {
      // A remembered folder is a convenience, not a requirement.
    }
    setProjectPath(selected);
  };

  const mutate = async (nextAction: 'connect' | 'disconnect') => {
    if (projectPath === null) return;
    setAction(nextAction);
    setError(null);
    try {
      const next = nextAction === 'connect'
        ? await localApi.agents.connectProvider(provider, projectPath)
        : await localApi.agents.disconnectProvider(provider, projectPath);
      setStatus(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Could not ${nextAction} ${providerLabel(provider)}.`);
    } finally {
      setAction(null);
    }
  };

  const label = providerLabel(provider);

  return (
    <section className="space-y-5 rounded-xl border border-border bg-card p-5" aria-labelledby="agent-hooks-title">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Eye className="h-4 w-4" aria-hidden="true" />
          </div>
          <div>
            <h2 id="agent-hooks-title" className="text-sm font-medium text-foreground">
              Agent run debugger
            </h2>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              Observe Codex and Claude Code lifecycle metadata in Agents → Runs.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void chooseProject()}
          className={cn(
            'inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs font-medium',
            'text-muted-foreground transition-colors hover:bg-accent hover:text-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          )}
        >
          <FolderOpen className="h-3.5 w-3.5" aria-hidden="true" />
          {projectPath === null ? 'Choose project' : 'Change project'}
        </button>
      </div>

      <div role="tablist" aria-label="Agent hook provider" className="flex gap-1">
        {(['codex', 'claude'] as const).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={provider === item}
            onClick={() => setProvider(item)}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              provider === item
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {providerLabel(item)}
          </button>
        ))}
      </div>

      {projectPath === null ? (
        <div className="rounded-lg border border-dashed border-border bg-background/50 p-4 text-center">
          <p className="text-sm font-medium text-foreground">Choose the project you run agents in</p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            Potluck will inspect the project-local config and show the exact change before writing.
          </p>
          {!ipc.isElectron && (
            <p className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">
              Folder selection is available in the Potluck desktop window.
            </p>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          <div className="rounded-lg border border-border bg-background px-3 py-2.5">
            <div className="flex items-center justify-between gap-3">
              <span className="text-[10px] font-medium uppercase tracking-hearth-label text-muted-foreground">
                Project
              </span>
              {status !== null && <StatusPill state={status.state} />}
            </div>
            <p className="mt-1 truncate font-mono text-[11px] text-foreground" title={projectPath}>
              {projectPath}
            </p>
            {status !== null && (
              <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={status.config_path}>
                {status.config_path}
              </p>
            )}
          </div>

          {isLoading ? (
            <div className="flex min-h-28 items-center justify-center gap-2 text-xs text-muted-foreground" aria-live="polite">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              Inspecting {label} config…
            </div>
          ) : status !== null ? (
            <ConnectorAction
              status={status}
              providerLabel={label}
              action={action}
              onConnect={() => void mutate('connect')}
              onDisconnect={() => void mutate('disconnect')}
            />
          ) : null}
        </div>
      )}

      {error !== null && (
        <p className="flex items-start gap-2 text-xs leading-relaxed text-destructive" role="alert">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {error}
        </p>
      )}

      <div className="flex items-start gap-2 rounded-lg border border-sage/25 bg-sage/5 p-3">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-sage" aria-hidden="true" />
        <p className="text-xs leading-relaxed text-muted-foreground">
          Metadata only. Potluck excludes prompts, responses, shell commands, tool arguments and
          results, file contents, transcripts, secrets, and environment variables. Agent work
          continues if Potluck is closed.
        </p>
      </div>
    </section>
  );
}

function ConnectorAction({
  status,
  providerLabel: label,
  action,
  onConnect,
  onDisconnect,
}: {
  status: AgentConnectorStatus;
  providerLabel: string;
  action: 'connect' | 'disconnect' | null;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  if (status.state === 'invalid') {
    return (
      <div className="rounded-lg border border-destructive/25 bg-destructive/5 p-3">
        <p className="text-xs font-medium text-destructive">Potluck left this file unchanged</p>
        <p className="mt-1 break-words text-xs leading-relaxed text-muted-foreground" role="alert">
          {status.error}
        </p>
      </div>
    );
  }

  if (status.state === 'unavailable') {
    return (
      <div className="rounded-lg border border-destructive/25 bg-destructive/5 p-3">
        <p className="text-xs font-medium text-destructive">This Potluck build is missing the bridge</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          Rebuild or update the desktop app before installing these hooks.
        </p>
      </div>
    );
  }

  if (status.state === 'installed') {
    return (
      <div className="space-y-3">
        <div className="rounded-lg border border-sage/25 bg-sage/5 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <CheckCircle2 className="h-3.5 w-3.5 text-sage" aria-hidden="true" />
            {status.installed_events.length} lifecycle hooks installed
          </p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            {status.provider === 'codex' ? (
              <>Open <code className="font-mono text-foreground">/hooks</code> in Codex to review and trust them, then start a new session.</>
            ) : (
              <>Start a new Claude Code session in this project. Its next run will appear in Agents → Runs.</>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={onDisconnect}
          disabled={action !== null}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-muted-foreground',
            'transition-colors hover:bg-destructive/10 hover:text-destructive',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            'disabled:cursor-not-allowed disabled:opacity-40',
          )}
        >
          {action === 'disconnect' ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Unplug className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          {action === 'disconnect' ? 'Disconnecting…' : `Disconnect ${label}`}
        </button>
        <ManualSetup status={status} />
      </div>
    );
  }

  const isRepair = status.state === 'partial';
  const eventCount = status.planned_connect_events.length;
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-background p-3">
        <p className="text-xs font-medium text-foreground">
          {isRepair ? 'Repair this connection' : `Connect ${label}`}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          Potluck will {status.config_exists ? 'merge' : 'create'} {eventCount} metadata-only lifecycle hooks in the file above. Existing settings and unrelated hooks stay intact.
        </p>
        {status.stale_events.length > 0 && (
          <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
            {status.stale_events.length} older Potluck hook {status.stale_events.length === 1 ? 'entry' : 'entries'} will be replaced.
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={onConnect}
        disabled={action !== null}
        className={cn(
          'inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground',
          'shadow-hearth-sm transition-colors hover:bg-primary/90',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'disabled:cursor-not-allowed disabled:opacity-40',
        )}
      >
        {action === 'connect' ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : isRepair ? (
          <RefreshCw className="h-4 w-4" aria-hidden="true" />
        ) : (
          <Plug className="h-4 w-4" aria-hidden="true" />
        )}
        {action === 'connect' ? 'Updating config…' : isRepair ? 'Repair hooks' : `Connect ${label}`}
      </button>
      <ManualSetup status={status} />
    </div>
  );
}

function ManualSetup({ status }: { status: AgentConnectorStatus }) {
  return (
    <details className="group rounded-lg border border-border/70 bg-background/50">
      <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        Manual setup
        <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="border-t border-border/70 p-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <p className="text-[11px] text-muted-foreground">Merge into the config file shown above.</p>
          <CopyButton value={status.manual_config} />
        </div>
        <pre className="max-h-56 overflow-auto rounded-md border border-border bg-background p-3 text-[10px] leading-relaxed text-foreground">
          <code>{status.manual_config}</code>
        </pre>
      </div>
    </details>
  );
}
