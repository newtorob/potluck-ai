/**
 * GatewayPanel - Settings, Connect tools.
 *
 * Turns Potluck into a drop-in OpenAI-compatible provider for external tools
 * (Cursor, Continue, aider, Open WebUI, the openai SDK). The user enables the
 * gateway, generates an API key, and copies the base URL + key into their tool.
 *
 * The key gates ONLY the /v1 gateway surface, never the rest of the sidecar.
 * Requests run on this machine's loaded model. The middleware reads these
 * settings live, so enabling / rotating takes effect with no restart.
 */

import { useState } from 'react';
import { Cable, Copy, Check, Eye, EyeOff, RefreshCw, KeyRound, ShieldCheck } from 'lucide-react';
import { cn } from '@/components/ui/cn';
import { localApi, type PotluckSettings, type PotluckSettingsPatch } from '@/lib/localApi';
import { AgentHooksPanel } from '@/components/settings/AgentHooksPanel';
import { FEATURES } from '@/lib/features';

// External tools connect to the sidecar's fixed loopback address.
const GATEWAY_BASE_URL = 'http://127.0.0.1:8321/v1';

interface GatewayPanelProps {
  settings: PotluckSettings;
  isSaving: boolean;
  saveError: string | null;
  onUpdate: (patch: PotluckSettingsPatch) => Promise<void>;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  return (
    <button
      type="button"
      onClick={() => void handleCopy()}
      aria-label={copied ? `${label} copied` : `Copy ${label}`}
      title={copied ? 'Copied' : 'Copy'}
      className={cn(
        'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border',
        'text-muted-foreground transition-colors hover:bg-muted hover:text-foreground',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
      )}
    >
      {copied ? <Check className="h-3.5 w-3.5 text-sage" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
    </button>
  );
}

export function GatewayPanel({ settings, isSaving, saveError, onUpdate }: GatewayPanelProps) {
  const gateway = settings.gateway;
  const hasKey = gateway.api_key.length > 0;

  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const enabledAndReady = gateway.enabled && hasKey;

  const setEnabled = (enabled: boolean) => {
    void onUpdate({ gateway: { enabled } });
  };

  const setPoolRouting = (pool_routing: boolean) => {
    void onUpdate({ gateway: { pool_routing } });
  };

  const generateKey = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await localApi.rotateGatewayKey();
      setRevealed(true);
      // Push the returned key straight into the store so the display can't show a
      // stale value (don't rely on a re-fetch race).
      await onUpdate({ gateway: { enabled: true, api_key: res.api_key } });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not generate a key.');
    } finally {
      setBusy(false);
    }
  };

  const maskedKey = hasKey
    ? revealed
      ? gateway.api_key
      : `${gateway.api_key.slice(0, 11)}${'•'.repeat(20)}`
    : '';

  const exampleModel = settings.active_model_id || 'your-model-slug';

  return (
    <div className="space-y-6">
      {FEATURES.agentWorkroom && <AgentHooksPanel />}

      {/* Intro banner */}
      <div className="flex items-start gap-3 rounded-xl border border-primary/20 bg-primary/5 px-4 py-3">
        <Cable className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
        <div>
          <p className="text-sm font-medium text-foreground">
            Use Potluck from the tools you already have.
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Potluck speaks the OpenAI API. Point any compatible tool (Cursor, Continue, aider,
            Open WebUI, the openai SDK) at the address below and it runs on this machine's loaded
            model. The key opens only this gateway, never the rest of Potluck.
          </p>
        </div>
      </div>

      {(saveError || error) && (
        <p className="text-sm text-destructive" role="alert">
          {saveError || error}
        </p>
      )}

      {/* Enable toggle */}
      <div className="flex items-start justify-between gap-4 border-b border-border py-4">
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-medium text-foreground">Enable the gateway</span>
          <p className="text-xs text-muted-foreground">
            {enabledAndReady
              ? 'On. External tools with the key can reach this machine on the address below.'
              : hasKey
                ? 'Off. Turn on to let tools with the key connect.'
                : 'Generate a key below to turn this on.'}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={gateway.enabled}
          aria-label="Enable the gateway"
          disabled={isSaving || !hasKey}
          onClick={() => setEnabled(!gateway.enabled)}
          className={cn(
            'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors duration-200',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-50',
            gateway.enabled ? 'bg-primary' : 'bg-muted',
          )}
        >
          <span
            className={cn(
              'pointer-events-none inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200',
              gateway.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]',
            )}
          />
        </button>
      </div>

      {/* Open-pool routing */}
      <div className="flex items-start justify-between gap-4 border-b border-border py-4">
        <div className="flex flex-col gap-0.5 pr-2">
          <span className="text-sm font-medium text-foreground">Allow open-pool routing</span>
          <p className="text-xs text-muted-foreground">
            When a tool asks for a model you do not have locally or on your own machines, send just
            that one prompt through the network to another member's machine. Your machines are
            always tried first. Off by default.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={gateway.pool_routing ?? false}
          aria-label="Allow open-pool routing"
          disabled={isSaving || !enabledAndReady}
          onClick={() => setPoolRouting(!(gateway.pool_routing ?? false))}
          className={cn(
            'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors duration-200',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
            'disabled:cursor-not-allowed disabled:opacity-50',
            gateway.pool_routing ? 'bg-primary' : 'bg-muted',
          )}
        >
          <span
            className={cn(
              'pointer-events-none inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform duration-200',
              gateway.pool_routing ? 'translate-x-[18px]' : 'translate-x-[3px]',
            )}
          />
        </button>
      </div>

      {/* Base URL */}
      <div className="space-y-1.5">
        <span className="text-xs font-medium uppercase tracking-hearth-label text-muted-foreground">
          Base URL
        </span>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-muted/50 px-3 py-2 font-mono text-[13px] text-foreground">
            {GATEWAY_BASE_URL}
          </code>
          <CopyButton value={GATEWAY_BASE_URL} label="base URL" />
        </div>
      </div>

      {/* API key */}
      <div className="space-y-1.5">
        <span className="text-xs font-medium uppercase tracking-hearth-label text-muted-foreground">
          API key
        </span>
        {hasKey ? (
          <>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-muted/50 px-3 py-2 font-mono text-[13px] text-foreground">
                {maskedKey}
              </code>
              <button
                type="button"
                onClick={() => setRevealed((r) => !r)}
                aria-label={revealed ? 'Hide key' : 'Reveal key'}
                title={revealed ? 'Hide' : 'Reveal'}
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {revealed ? <EyeOff className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
              </button>
              <CopyButton value={gateway.api_key} label="API key" />
            </div>
            <button
              type="button"
              onClick={() => void generateKey()}
              disabled={busy}
              className="mt-1 inline-flex items-center gap-1.5 rounded text-xs text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <RefreshCw className={cn('h-3 w-3', busy && 'animate-spin')} aria-hidden />
              Regenerate key
            </button>
            <p className="text-[11px] text-muted-foreground">
              Regenerating invalidates the old key. Any tool using it will need the new one.
            </p>
          </>
        ) : (
          <button
            type="button"
            onClick={() => void generateKey()}
            disabled={busy}
            className={cn(
              'inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground',
              'transition-colors hover:bg-primary/90 disabled:opacity-50 shadow-hearth-sm',
            )}
          >
            <KeyRound className={cn('h-4 w-4', busy && 'animate-pulse')} aria-hidden />
            Generate API key
          </button>
        )}
      </div>

      {/* How to connect */}
      <div className="rounded-xl border border-border bg-card p-4">
        <p className="mb-2 text-sm font-medium text-foreground">How to connect</p>
        <ol className="space-y-1.5 text-xs text-muted-foreground">
          <li>1. Generate a key and turn the gateway on.</li>
          <li>
            2. In your tool's model settings, choose the OpenAI-compatible / custom provider and set
            the base URL and key above.
          </li>
          <li>
            3. Use an installed model id as the model name, for example{' '}
            <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px] text-foreground">
              {exampleModel}
            </code>
            .
          </li>
        </ol>
      </div>

      {/* Privacy note */}
      <div className="flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-sage" aria-hidden />
        <span>
          The gateway is reachable only from this machine (loopback), and the key authorizes the
          gateway alone. Requests run on your loaded model by default. A request for a model you keep
          on another of your own machines runs there over your private mesh. Open-pool routing, only
          when you enable it above, sends a single prompt to another member's machine with no account
          or identity attached.
        </span>
      </div>
    </div>
  );
}
