import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Activity,
  AlertCircle,
  Bot,
  Check,
  CircleStop,
  ExternalLink,
  FilePenLine,
  FolderOpen,
  Loader2,
  Play,
  Send,
  ShieldAlert,
  Sparkles,
  TerminalSquare,
} from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { ipc } from '@/lib/ipc';
import { localApi } from '@/lib/localApi';
import type {
  ManagedAgentApproval,
  ManagedAgentCapabilities,
  ManagedAgentSession,
  ManagedAgentStatus,
  ManagedApprovalDecision,
  ManagedSandboxMode,
} from '@/types/agents';
import type { TaskState } from '@/types/tasks';

const PROJECT_STORAGE_KEY = 'potluck:agent-hook-project';

function savedProjectPath(): string | null {
  try {
    return localStorage.getItem(PROJECT_STORAGE_KEY);
  } catch {
    return null;
  }
}

const STATUS_LABEL: Record<ManagedAgentStatus, string> = {
  starting: 'Starting',
  running: 'Working',
  waiting_approval: 'Needs approval',
  idle: 'Ready',
  interrupted: 'Interrupted',
  failed: 'Failed',
  stopped: 'Stopped',
};

const STATUS_STYLE: Record<ManagedAgentStatus, string> = {
  starting: 'border-primary/25 bg-primary/10 text-primary',
  running: 'border-sage/30 bg-sage/10 text-sage-foreground',
  waiting_approval: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  idle: 'border-border bg-muted text-muted-foreground',
  interrupted: 'border-border bg-muted text-muted-foreground',
  failed: 'border-destructive/30 bg-destructive/10 text-destructive',
  stopped: 'border-border bg-muted text-muted-foreground',
};

function StatusPill({ status }: { status: ManagedAgentStatus }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-medium',
        STATUS_STYLE[status],
      )}
    >
      {(status === 'starting' || status === 'running') && (
        <span className="h-1.5 w-1.5 animate-hearth-pulse rounded-full bg-current" aria-hidden="true" />
      )}
      {STATUS_LABEL[status]}
    </span>
  );
}

function elapsed(ms: number | null): string | null {
  if (ms === null) return null;
  if (ms < 1_000) return `${ms}ms`;
  return `${(ms / 1_000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

function approvalSupports(approval: ManagedAgentApproval, decision: ManagedApprovalDecision): boolean {
  return approval.available_decisions.length === 0 || approval.available_decisions.includes(decision);
}

interface ApprovalCardProps {
  approval: ManagedAgentApproval;
  busy: boolean;
  onResolve: (decision: ManagedApprovalDecision) => void;
}

function ApprovalCard({ approval, busy, onResolve }: ApprovalCardProps) {
  return (
    <section
      className="rounded-xl border border-amber-500/30 bg-amber-500/8 p-4 shadow-hearth-sm"
      aria-labelledby={`approval-${approval.id}`}
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 rounded-lg bg-amber-500/15 p-2 text-amber-700 dark:text-amber-300">
          <ShieldAlert className="h-4 w-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 id={`approval-${approval.id}`} className="text-sm font-semibold text-foreground">
            {approval.title}
          </h3>
          {approval.reason && (
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{approval.reason}</p>
          )}
        </div>
      </div>

      {approval.command && (
        <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-border/60 bg-background/80 p-3 font-mono text-[11px] leading-relaxed text-foreground">
          {approval.command}
        </pre>
      )}
      {approval.cwd && (
        <p className="mt-2 truncate font-mono text-[10px] text-muted-foreground" title={approval.cwd}>
          {approval.cwd}
        </p>
      )}

      <div className="mt-3 flex flex-wrap justify-end gap-2">
        {approvalSupports(approval, 'decline') && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve('decline')}
            className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            Deny
          </button>
        )}
        {approvalSupports(approval, 'acceptForSession') && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve('acceptForSession')}
            className="rounded-lg border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            Allow for session
          </button>
        )}
        {approvalSupports(approval, 'accept') && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onResolve('accept')}
            className="flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground shadow-hearth-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Check className="h-3.5 w-3.5" aria-hidden="true" />}
            Allow once
          </button>
        )}
      </div>
    </section>
  );
}

interface TaskWorkroomProps {
  task: TaskState;
  onTaskRefresh: () => void;
}

export function TaskWorkroom({ task, onTaskRefresh }: TaskWorkroomProps) {
  const navigate = useNavigate();
  const [capabilities, setCapabilities] = useState<ManagedAgentCapabilities | null>(null);
  const [session, setSession] = useState<ManagedAgentSession | null>(null);
  const [projectPath, setProjectPath] = useState<string | null>(savedProjectPath);
  const [model, setModel] = useState('');
  const [sandbox, setSandbox] = useState<ManagedSandboxMode>('workspace-write');
  const [direction, setDirection] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<'start' | 'send' | 'interrupt' | 'approval' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionId = session?.id ?? null;
  const messageCount = session?.messages.length ?? 0;
  const lastMessageText = session?.messages.at(-1)?.text;

  const replaceSession = useCallback((next: ManagedAgentSession) => {
    setSession(next);
    setError(null);
  }, []);

  useEffect(() => {
    let live = true;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const [nextCapabilities, sessions] = await Promise.all([
          localApi.agents.managedCapabilities(),
          localApi.agents.listManagedSessions(task.id),
        ]);
        if (!live) return;
        setCapabilities(nextCapabilities);
        setSession(sessions[0] ?? null);
      } catch (reason) {
        if (live) setError(reason instanceof Error ? reason.message : 'Could not open the workroom.');
      } finally {
        if (live) setLoading(false);
      }
    };
    void load();
    return () => { live = false; };
  }, [task.id]);

  useEffect(() => {
    if (sessionId === null) return;
    let live = true;
    let timer: number | null = null;
    const poll = async () => {
      try {
        const next = await localApi.agents.getManagedSession(sessionId);
        if (live) setSession(next);
      } catch (reason) {
        if (live) setError(reason instanceof Error ? reason.message : 'The managed session disconnected.');
      } finally {
        if (live) timer = window.setTimeout(() => { void poll(); }, 900);
      }
    };
    timer = window.setTimeout(() => { void poll(); }, 900);
    return () => {
      live = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [sessionId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView?.({ block: 'end' });
  }, [lastMessageText, messageCount]);

  useEffect(() => {
    if (session?.status !== 'running' && session?.status !== 'waiting_approval') return;
    const interval = window.setInterval(onTaskRefresh, 5_000);
    return () => window.clearInterval(interval);
  }, [onTaskRefresh, session?.status]);

  const chooseProject = async () => {
    if (!ipc.isElectron) {
      setError('Repository selection is available in the Potluck desktop app.');
      return;
    }
    const selected = await ipc.selectDirectory();
    if (selected === null) return;
    try {
      localStorage.setItem(PROJECT_STORAGE_KEY, selected);
    } catch {
      // Remembering the folder is optional.
    }
    setProjectPath(selected);
    setError(null);
  };

  const start = async () => {
    if (projectPath === null) {
      setError('Choose the repository folder Codex should work in.');
      return;
    }
    setAction('start');
    setError(null);
    try {
      replaceSession(await localApi.agents.startManagedSession({
        task_id: task.id,
        cwd: projectPath,
        sandbox,
        ...(model.trim() ? { model: model.trim() } : {}),
        ...(direction.trim() ? { direction: direction.trim() } : {}),
      }));
      setDirection('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not start Codex.');
    } finally {
      setAction(null);
    }
  };

  const sendMessage = async () => {
    if (session === null || !message.trim()) return;
    const text = message.trim();
    setAction('send');
    setError(null);
    try {
      const isActive = session.status === 'running' || session.status === 'waiting_approval';
      const next = isActive
        ? await localApi.agents.steerManagedSession(session.id, text)
        : await localApi.agents.sendManagedMessage(session.id, text);
      replaceSession(next);
      setMessage('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not send guidance.');
    } finally {
      setAction(null);
    }
  };

  const interrupt = async () => {
    if (session === null) return;
    setAction('interrupt');
    setError(null);
    try {
      replaceSession(await localApi.agents.interruptManagedSession(session.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not interrupt Codex.');
    } finally {
      setAction(null);
    }
  };

  const resolveApproval = async (approvalId: string, decision: ManagedApprovalDecision) => {
    if (session === null) return;
    setAction('approval');
    setError(null);
    try {
      replaceSession(await localApi.agents.resolveManagedApproval(session.id, approvalId, decision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not resolve the approval.');
    } finally {
      setAction(null);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center text-muted-foreground" role="status">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> Opening workroom
      </div>
    );
  }

  if (session === null) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl">
          <div className="rounded-2xl border border-border bg-card p-6 shadow-hearth-sm">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10 text-primary">
              <TerminalSquare className="h-5 w-5" aria-hidden="true" />
            </div>
            <h2 className="mt-4 font-serif text-xl tracking-display text-foreground">Start a Codex workroom</h2>
            <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
              Potluck will give Codex this task, show its live work here, and keep approvals and task controls within reach.
            </p>

            {error && (
              <p className="mt-4 rounded-lg border border-destructive/25 bg-destructive/8 px-3 py-2 text-xs text-destructive" role="alert">
                {error}
              </p>
            )}

            {capabilities?.available === false && (
              <div className="mt-4 rounded-lg border border-amber-500/25 bg-amber-500/8 p-3 text-xs text-amber-700 dark:text-amber-300">
                {capabilities.error ?? 'Install and sign in to the Codex CLI before starting a workroom.'}
              </div>
            )}

            <div className="mt-6 space-y-4">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-foreground">Repository folder</label>
                <button
                  type="button"
                  onClick={() => void chooseProject()}
                  className="flex w-full items-center gap-3 rounded-lg border border-input bg-background px-3 py-2.5 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <FolderOpen className="h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
                  <span className={cn('min-w-0 flex-1 truncate font-mono text-xs', projectPath ? 'text-foreground' : 'text-muted-foreground')}>
                    {projectPath ?? 'Choose a repository'}
                  </span>
                </button>
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label htmlFor="workroom-sandbox" className="text-xs font-medium text-foreground">File access</label>
                  <select
                    id="workroom-sandbox"
                    value={sandbox}
                    onChange={(event) => setSandbox(event.target.value as ManagedSandboxMode)}
                    className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  >
                    <option value="workspace-write">Allow workspace edits</option>
                    <option value="read-only">Read only</option>
                  </select>
                </div>
                <div className="space-y-1.5">
                  <label htmlFor="workroom-model" className="text-xs font-medium text-foreground">Model</label>
                  <input
                    id="workroom-model"
                    value={model}
                    onChange={(event) => setModel(event.target.value)}
                    placeholder="Use Codex default"
                    className="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>
              </div>

              <div className="space-y-1.5">
                <label htmlFor="workroom-direction" className="text-xs font-medium text-foreground">Starting direction <span className="font-normal text-muted-foreground">(optional)</span></label>
                <textarea
                  id="workroom-direction"
                  value={direction}
                  onChange={(event) => setDirection(event.target.value)}
                  rows={3}
                  placeholder="Begin with the first pending step, or add specific guidance."
                  className="w-full resize-none rounded-lg border border-input bg-background px-3 py-2 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>
            </div>

            <div className="mt-5 flex items-center justify-between gap-4">
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                Content stays local. Run history stores metadata, not prompts, commands, results, or file contents.
              </p>
              <button
                type="button"
                disabled={action === 'start' || capabilities?.available === false}
                onClick={() => void start()}
                className="flex shrink-0 items-center gap-2 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground shadow-hearth-sm transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                {action === 'start' ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}
                Start Codex
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  const isActive = session.status === 'running' || session.status === 'waiting_approval' || session.status === 'starting';

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-background">
      <div className="flex min-h-16 shrink-0 items-center justify-between gap-4 border-b border-border px-5 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-foreground">Codex workroom</h2>
            <StatusPill status={session.status} />
            <span className="rounded-full border border-primary/20 bg-primary/5 px-2 py-0.5 text-[10px] font-medium text-primary">Managed by Potluck</span>
          </div>
          <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground" title={session.cwd}>
            {session.cwd} · {session.model ?? 'Codex default'} · {session.sandbox}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => navigate('/agents')}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" /> Trace
          </button>
          {isActive && session.active_turn_id !== null && (
            <button
              type="button"
              disabled={action === 'interrupt'}
              onClick={() => void interrupt()}
              className="flex items-center gap-1.5 rounded-lg border border-destructive/25 px-2.5 py-1.5 text-xs font-medium text-destructive transition-colors hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              {action === 'interrupt' ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <CircleStop className="h-3.5 w-3.5" aria-hidden="true" />}
              Interrupt
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mx-5 mt-4 flex items-start gap-2 rounded-lg border border-destructive/25 bg-destructive/8 px-3 py-2 text-xs text-destructive" role="alert">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" /> {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5" aria-live="polite" aria-label="Codex conversation">
            {session.approvals.map((approval) => (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                busy={action === 'approval'}
                onResolve={(decision) => { void resolveApproval(approval.id, decision); }}
              />
            ))}

            {session.messages.length === 0 && (
              <div className="flex h-full flex-col items-center justify-center text-center text-muted-foreground">
                <Bot className="h-8 w-8 opacity-30" aria-hidden="true" />
                <p className="mt-2 text-sm">Codex is preparing this task.</p>
              </div>
            )}

            {session.messages.map((entry) => (
              <article key={entry.id} className={cn('flex gap-3', entry.role === 'user' && 'justify-end')}>
                {entry.role === 'assistant' && (
                  <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <Bot className="h-3.5 w-3.5" aria-hidden="true" />
                  </div>
                )}
                <div className={cn(
                  'max-w-[82%] rounded-xl px-3.5 py-2.5 text-sm leading-relaxed',
                  entry.role === 'user' ? 'bg-primary text-primary-foreground' : 'border border-border/60 bg-card text-foreground',
                )}>
                  {entry.kind === 'steer' && <p className="mb-1 text-[10px] font-medium uppercase tracking-wide opacity-70">Steer</p>}
                  <p className="whitespace-pre-wrap break-words">{entry.text}</p>
                  {entry.status === 'streaming' && <span className="ml-1 inline-block h-3 w-1 animate-pulse bg-current align-middle" aria-hidden="true" />}
                </div>
              </article>
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div className="shrink-0 border-t border-border p-4">
            <div className="flex items-end gap-2 rounded-xl border border-input bg-card p-2 focus-within:border-primary/40 focus-within:ring-2 focus-within:ring-ring/40">
              <textarea
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
                rows={2}
                placeholder={isActive ? 'Steer the active turn…' : 'Give Codex the next direction…'}
                aria-label={isActive ? 'Steer the active turn' : 'Send follow-up direction'}
                className="min-h-10 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed text-foreground placeholder:text-muted-foreground focus:outline-none"
              />
              <button
                type="button"
                disabled={!message.trim() || action === 'send' || session.status === 'starting'}
                onClick={() => void sendMessage()}
                aria-label={isActive ? 'Steer Codex' : 'Send to Codex'}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
              >
                {action === 'send' ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : isActive ? <Sparkles className="h-4 w-4" aria-hidden="true" /> : <Send className="h-4 w-4" aria-hidden="true" />}
              </button>
            </div>
            <p className="mt-1.5 text-[10px] text-muted-foreground">
              {isActive ? 'Guidance is added to the current turn.' : 'This starts the next turn in the same Codex thread.'}
            </p>
          </div>
        </main>

        <aside className="hidden w-72 shrink-0 flex-col border-l border-border bg-surface-1 md:flex" aria-label="Live activity">
          <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border/60 px-4 font-serif text-[11px] italic tracking-hearth-label text-muted-foreground">
            <Activity className="h-3.5 w-3.5" aria-hidden="true" /> Live activity
          </div>
          <div className="flex-1 space-y-2 overflow-y-auto p-3">
            {session.items.length === 0 && (
              <p className="px-2 py-4 text-center text-xs text-muted-foreground">Tool and file activity will appear here.</p>
            )}
            {session.items.slice().reverse().map((item) => (
              <article key={item.id} className="rounded-lg border border-border/60 bg-card p-2.5">
                <div className="flex items-start gap-2">
                  <div className="mt-0.5 text-muted-foreground">
                    {item.item_type === 'fileChange' ? <FilePenLine className="h-3.5 w-3.5" aria-hidden="true" /> : item.item_type === 'commandExecution' ? <TerminalSquare className="h-3.5 w-3.5" aria-hidden="true" /> : <Activity className="h-3.5 w-3.5" aria-hidden="true" />}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium text-foreground">{item.label}</p>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
                      <span className="capitalize">{item.status}</span>
                      {elapsed(item.duration_ms) && <span>{elapsed(item.duration_ms)}</span>}
                    </div>
                  </div>
                  {item.status === 'running' && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" aria-hidden="true" />}
                </div>
                {item.detail && (
                  <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/60 p-2 font-mono text-[10px] leading-relaxed text-muted-foreground">{item.detail}</pre>
                )}
              </article>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}
