import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, Layers, Monitor, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { localApi } from '@/lib/localApi';
import type { CatalogModel } from '@/stores/modelStore';
import type { ModelAvailability, SplitSelection, SplitStatus } from '@/types/modelAvailability';

const button = 'rounded-lg border border-border px-3 py-2 text-xs font-medium transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50';
const field = 'rounded-lg border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50';

/** Owned-machine discovery only. Viewing this panel never enables sharing or
 * network routing. A split starts only after an exact-artifact plan preview. */
export function MachineModels({ models }: { models: CatalogModel[] }) {
  const [data, setData] = useState<ModelAvailability | null>(null);
  const [status, setStatus] = useState<SplitStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [model, setModel] = useState('');
  const [peers, setPeers] = useState<string[]>([]);
  const [context, setContext] = useState(2048);
  const [plan, setPlan] = useState<SplitSelection | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const previous = useRef<Set<string> | null>(null);
  const installed = models.filter(m => m.installed);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [inventory, session] = await Promise.all([
          localApi.modelAvailability(controller.signal), localApi.splitStatus(controller.signal),
        ]);
        if (controller.signal.aborted) return;
        const keys = new Set(inventory.models.filter(m => !m.local).map(m => `${m.id}:${m.sha256}`));
        const added = previous.current ? [...keys].filter(key => !previous.current?.has(key)).length : 0;
        const removed = previous.current ? [...previous.current].filter(key => !keys.has(key)).length : 0;
        if (added || removed) setNotice(`${added ? `${added} model version${added === 1 ? '' : 's'} became available. ` : ''}${removed ? `${removed} no longer available.` : ''}`);
        previous.current = keys;
        setData(inventory); setStatus(session); setError(null);
      } catch {
        if (!controller.signal.aborted) {
          setData(null); setPlan(null);
          setError('Could not check your machines. Check the runtime and connection, then refresh.');
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 5000);
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);

  const workers = data?.machines.filter(m => m.splitReady) ?? [];
  const validSelection = !!model && installed.some(m => m.slug === model) && peers.length > 0 && peers.every(ip => workers.some(w => w.meshIp === ip));
  const active = status && ['loading', 'ready', 'stopping'].includes(status.state);
  async function preview() {
    setBusy('Verifying model…'); setActionError(null); setPlan(null);
    try {
      const selection = { model: await localApi.modelIdentity(model), peers: [...peers], context };
      await localApi.splitPlan(selection);
      setPlan(selection);
    } catch (e) { setActionError(e instanceof Error ? e.message : 'Could not prepare this split.'); }
    finally { setBusy(null); }
  }
  async function start() {
    if (!plan) return;
    setBusy('Starting…'); setActionError(null);
    try { setStatus(await localApi.splitStart(plan)); setPlan(null); setRefresh(n => n + 1); }
    catch (e) { setActionError(e instanceof Error ? e.message : 'Could not start this split.'); setPlan(null); }
    finally { setBusy(null); }
  }
  async function stop() {
    setBusy('Stopping…'); setActionError(null);
    try { setStatus(await localApi.splitStop()); setPlan(null); setRefresh(n => n + 1); }
    catch { setActionError('Could not stop the split. Check the runtime and try again.'); }
    finally { setBusy(null); }
  }

  return <section aria-labelledby="machine-models-title" className="mb-6 rounded-xl border border-border bg-card p-5 sm:p-6">
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="mb-1 flex items-center gap-2 text-xs text-muted-foreground"><Monitor className="h-4 w-4" aria-hidden="true" /> Your machines</p>
        <h2 id="machine-models-title" className="text-2xl">More computers. More possibilities.</h2>
        <p className="mt-2 max-w-2xl text-sm text-muted-foreground">See what your connected computers can serve right now, or prepare one model to run across them.</p>
      </div>
      <button className={button} onClick={() => setRefresh(n => n + 1)} aria-label="Refresh machine models" title="Refresh"><RefreshCw className="h-4 w-4" aria-hidden="true" /></button>
    </div>
    <p role="status" className="mt-3 text-xs text-muted-foreground">{notice}</p>
    {error ? <p role="alert" className="mt-3 text-sm text-destructive">{error}</p> : !data ?
      <p role="status" className="mt-4 rounded-lg bg-muted/50 p-4 text-sm text-muted-foreground">Checking models and split workers…</p> : <>
      <div className="my-5 grid grid-cols-2 gap-3">
        <div className="rounded-lg bg-muted/40 p-4"><p className="text-3xl tabular-nums">+{data.additionalModels}</p><p className="mt-1 text-xs text-muted-foreground">model choices beyond this computer</p></div>
        <div className="rounded-lg bg-muted/40 p-4"><p className="text-3xl tabular-nums">{data.splitWorkerCount}</p><p className="mt-1 text-xs text-muted-foreground">workers ready for a split</p></div>
      </div>
      <h3 className="text-lg">Ready now</h3>
      {data.models.length ? <ul className="mt-2 divide-y divide-border">{data.models.map(m => <li key={`${m.id}:${m.sha256}`} className="flex flex-wrap items-center justify-between gap-2 py-3">
        <div className="min-w-0"><p className="break-all text-sm font-medium">{m.id}</p><p className="mt-1 break-words text-xs text-muted-foreground">{[m.local ? 'This computer' : '', ...m.machines.map(machine => machine.name)].filter(Boolean).join(' · ')}</p></div>
        <div className="text-xs text-muted-foreground"><span className="font-mono" title={m.sha256 ?? undefined}>{m.sha256?.slice(0, 12) ?? 'Version unverified'}</span>{m.machines.length > 0 && <span className="ml-3">{m.idleMachineCount} remote idle</span>}</div>
      </li>)}</ul> : <p className="mt-2 text-sm text-muted-foreground">Load a model below, or load one on a paired computer and enable its sharing.</p>}
      <p className="mt-3 text-xs text-muted-foreground">Choose “My machines” when sending a request to use these models. Project model pins still apply.</p>
      {data.truncated && <p className="mt-2 text-xs text-muted-foreground">Showing the first 32 reachable machines.</p>}
      <details className="mt-5 border-t border-border pt-4">
        <summary className="cursor-pointer rounded text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Run one model across computers</summary>
        <p className="mt-3 text-sm text-muted-foreground">The head keeps the model file; selected workers receive model layers. Available memory adds potential capacity. Placement is verified after loading; speed depends on the machines and connection.</p>
        {!data.splitEngineAvailable ? <p className="mt-3 text-sm">Install a split-capable Potluck runtime on this computer first.</p> : <fieldset disabled={!!busy || !!active} className="mt-4 space-y-4 disabled:opacity-60">
          <div className="flex flex-wrap gap-4">
            <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">Installed model
              <select className={field} value={model} onChange={e => { setModel(e.target.value); setPlan(null); }}><option value="">Select a model</option>{installed.map(m => <option key={m.slug} value={m.slug}>{m.name}</option>)}</select>
            </label>
            <label className="flex flex-col gap-1 text-xs">Context
              <select className={field} value={context} onChange={e => { setContext(Number(e.target.value)); setPlan(null); }}>{[512, 1024, 2048, 4096, 8192].map(n => <option key={n} value={n}>{n.toLocaleString()} tokens</option>)}</select>
            </label>
          </div>
          <div><p className="mb-2 text-xs font-medium">Owned workers · choose up to three</p>
            {workers.map(w => <label key={w.meshIp} className="mr-4 inline-flex items-center gap-2 py-2 text-sm"><input type="checkbox" checked={peers.includes(w.meshIp)} disabled={!peers.includes(w.meshIp) && peers.length >= 3} onChange={e => { setPeers(e.target.checked ? [...peers, w.meshIp] : peers.filter(ip => ip !== w.meshIp)); setPlan(null); }} className="accent-primary focus-visible:ring-2 focus-visible:ring-ring" />{w.name} <span className="text-xs text-muted-foreground">{w.backend}</span></label>)}
            {!workers.length && <p className="text-sm text-muted-foreground">On a paired computer, unload its model and run <code className="font-mono">potluck split worker on</code>.</p>}
          </div>
          <button className={button} disabled={!validSelection} onClick={() => void preview()}>{busy ?? 'Preview split'}</button>
        </fieldset>}
        {plan && validSelection && !active && <div className="mt-4 rounded-lg border border-primary/30 bg-primary/5 p-4">
          <p className="flex items-center gap-2 text-sm font-medium"><Check className="h-4 w-4" aria-hidden="true" /> Ready to attempt this split</p>
          <p className="mt-2 break-all text-xs font-mono">{plan.model.id} · SHA-256 {plan.model.sha256}</p>
          <p className="mt-2 text-sm">This computer + {peers.map(ip => workers.find(w => w.meshIp === ip)?.name).join(', ')} · {plan.context.toLocaleString()} tokens</p>
          <p className="mt-2 text-xs text-muted-foreground">Unload any current local model before starting. This starts a split session on your machines. It does not edit any project pin.</p>
          <button className={`${button} mt-3 bg-primary text-primary-foreground hover:bg-primary/90`} disabled={!!busy} onClick={() => void start()}>{busy ?? 'Start split'}<ArrowRight className="ml-2 inline h-3 w-3" aria-hidden="true" /></button>
        </div>}
      </details>
    </>}
    {status?.session && <div className="mt-5 rounded-lg border border-border p-4">
      <p className="flex items-center gap-2 text-sm font-medium"><Layers className="h-4 w-4" aria-hidden="true" />{status.session.model.id} · {status.session.phase ?? status.state}{error ? ' (last known)' : ''}</p>
      <p className="mt-2 text-xs text-muted-foreground">{status.session.elapsedSeconds ?? 0}s elapsed · {(status.session.workers.reduce((sum, w) => sum + w.sentBytes, 0) / 1048576).toFixed(1)} MiB sent to workers{status.session.loadSeconds != null ? ` · loaded in ${status.session.loadSeconds}s` : ''}</p>
      {status.session.workers.map(w => <p key={w.name} className="mt-2 text-xs">{w.name} · {w.modelMiB > 0 ? `${w.modelMiB.toFixed(1)} MiB of model placed` : 'Placement not yet verified'}</p>)}
      {status.state === 'ready' && <p className="mt-3 text-sm">Placement verified. The model is ready on this computer’s runtime.</p>}
      <button className={`${button} mt-3`} disabled={!!busy} onClick={() => void stop()}>{busy === 'Stopping…' ? busy : 'Stop split'}</button>
    </div>}
    {(actionError || status?.error) && <p className="mt-3 text-sm text-destructive" role="alert">{actionError || status?.error}</p>}
    <Link to="/household" className="mt-5 inline-block rounded text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Manage your connected computers</Link>
  </section>;
}
