import { useEffect, useId, useRef, useState } from 'react';
import { localApi } from '@/lib/localApi';
import { ipc } from '@/lib/ipc';
import type { PilotModelSetup } from '@/types/companyBrainPilot';
import { investigationButton as button, investigationField as field } from './controls';

function bytes(value: number | null): string {
  return value === null ? 'Unavailable' : `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

/** Explicit, session-only setup; source review and saved answers do not depend on it. */
export function CompanyBrainPilotSetup({
  disabled = false,
  activeModelId,
  connectionState,
  onModelChange,
  onBusyChange,
}: {
  disabled?: boolean;
  activeModelId?: string | null;
  connectionState?: 'checking' | 'offline' | 'connected';
  onModelChange: (modelId: string | null) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const id = useId();
  const runtimeUnavailable = connectionState === 'checking' || connectionState === 'offline';
  const [setup, setSetup] = useState<PilotModelSetup | null>(null);
  const [path, setPath] = useState('');
  const [importConsent, setImportConsent] = useState(false);
  const [prepareConsent, setPrepareConsent] = useState(false);
  const [busy, setBusy] = useState<string | null>('Checking setup');
  const [error, setError] = useState('');
  const alive = useRef(false);
  const lock = useRef(false);
  const generation = useRef(0);
  const externalBusy = useRef(false);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const lastActive = useRef<string | null | undefined>(undefined);
  function applySetup(result: PilotModelSetup) {
    if (lastActive.current !== undefined && lastActive.current !== result.active_model)
      setPrepareConsent(false);
    lastActive.current = result.active_model;
    setSetup(result);
  }
  function setExternalBusy(value: boolean) {
    if (externalBusy.current === value) return;
    externalBusy.current = value;
    busyCallback.current?.(value);
  }

  useEffect(() => {
    alive.current = true;
    const current = ++generation.current;
    localApi
      .companyBrainModelSetup()
      .then(
        (result) => {
          if (alive.current && generation.current === current) applySetup(result);
        },
        (err: unknown) => {
          if (alive.current && generation.current === current)
            setError(err instanceof Error ? err.message : 'Could not check model setup.');
        },
      )
      .finally(() => {
        if (alive.current && generation.current === current) setBusy(null);
      });
    return () => {
      alive.current = false;
      generation.current++;
      setExternalBusy(false);
    };
  }, []);

  async function action(
    label: string,
    work: () => Promise<PilotModelSetup>,
    completed?: (result: PilotModelSetup) => void,
    mutation = true,
  ) {
    if (lock.current || disabled || busy || (mutation && runtimeUnavailable)) return;
    lock.current = true;
    if (mutation) setExternalBusy(true);
    const current = ++generation.current;
    setBusy(label);
    setError('');
    try {
      const result = await work();
      if (!alive.current || generation.current !== current) return;
      applySetup(result);
      completed?.(result);
    } catch (err) {
      if (alive.current && generation.current === current)
        setError(
          err instanceof Error
            ? err.message
            : `${label} failed. Refresh setup before trying again.`,
        );
    } finally {
      lock.current = false;
      if (mutation) setExternalBusy(false);
      if (alive.current && generation.current === current) setBusy(null);
    }
  }
  async function pickFile() {
    if (lock.current || disabled || busy) return;
    lock.current = true;
    setExternalBusy(true);
    const current = ++generation.current;
    setBusy('Choosing file');
    setError('');
    try {
      const selected = await ipc.selectPilotModelFile();
      if (alive.current && generation.current === current && selected !== null) {
        setPath(selected);
        setImportConsent(false);
      }
    } catch (err) {
      if (alive.current && generation.current === current)
        setError(err instanceof Error ? err.message : 'Could not choose a model file.');
    } finally {
      lock.current = false;
      setExternalBusy(false);
      if (alive.current && generation.current === current) setBusy(null);
    }
  }
  const unavailable = disabled || !!busy || runtimeUnavailable;
  const modelStateChanged =
    setup !== null && activeModelId !== undefined && setup.active_model !== activeModelId;
  useEffect(() => {
    setPrepareConsent(false);
  }, [activeModelId, connectionState]);
  return (
    <section
      aria-label="Company Brain model setup"
      className="space-y-4 rounded-md border border-border p-4"
    >
      <div className="space-y-1">
        <h3 className="font-serif text-xl">Local model setup</h3>
        <p className="text-sm text-muted-foreground">
          Review sources and saved answers without a model. To ask a question, prepare the supplied
          model on this machine. The model file is provided separately from the installer; it is not
          uploaded or downloaded by this setup.
        </p>
      </div>
      {busy && (
        <p role="status" className="text-sm">
          {busy}…
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error} Your entries are kept. Refresh setup to check the current state.
        </p>
      )}
      {!setup && !busy && (
        <p className="text-sm">Model setup is unavailable. Refresh to try again.</p>
      )}
      {setup && (
        <div className="space-y-4">
          <p className="text-sm font-medium">
            {connectionState === 'checking'
              ? 'Checking local engine'
              : connectionState === 'offline'
                ? 'Local engine offline; check connection'
                : modelStateChanged
                  ? 'Model state changed; refresh setup'
                  : setup.ready
                    ? 'Ready for local questions'
                    : setup.installed
                      ? setup.artifact_verified
                        ? 'Model imported; not ready'
                        : 'Model file present; preparation verifies it'
                      : 'Model file needed'}
          </p>
          {modelStateChanged && (
            <p role="status" className="text-sm">
              The loaded model changed since the last check. Refresh setup before preparing or
              ending a model session.
            </p>
          )}
          <p className="text-sm text-muted-foreground">
            This pilot uses CPU inference and an 8K context for this session. Preparation may take
            time and requires available memory. Answers still need review; CPU speed depends on the
            machine.
          </p>
          <details className="rounded-md border border-border p-3">
            <summary className="cursor-pointer font-medium focus-visible:ring-2 focus-visible:ring-ring">
              Model details and resources
            </summary>
            <div className="mt-3 space-y-1 break-words text-sm">
              <p>{setup.model.name}</p>
              <p>
                File size: {bytes(setup.model.size_bytes)} (
                {setup.model.size_bytes.toLocaleString()} bytes)
              </p>
              <p className="break-all">SHA-256: {setup.model.sha256}</p>
              <p>
                Estimated RAM: {bytes(setup.resources.estimated_ram_bytes)}; available:{' '}
                {bytes(setup.resources.available_ram_bytes)}
              </p>
              <p>
                Required disk: {bytes(setup.resources.required_disk_bytes)}; free:{' '}
                {bytes(setup.resources.disk_free_bytes)}
              </p>
            </div>
          </details>
          {setup.issues.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {setup.issues.map((issue, index) => (
                <li key={`${issue.code}-${index}`}>{issue.message}</li>
              ))}
            </ul>
          )}
          {setup.warnings.map((warning, index) => (
            <p key={index} className="text-sm text-muted-foreground">
              {warning}
            </p>
          ))}
          {!setup.platform_supported && (
            <p className="text-sm">
              This platform is not supported for the local pilot model. You can still review sources
              and saved answers.
            </p>
          )}
          {!setup.installed && (
            <fieldset
              disabled={unavailable || !setup.can_import}
              className="space-y-3 disabled:opacity-60"
            >
              <legend className="text-sm font-medium">Import supplied GGUF</legend>
              {ipc.platform === 'browser' ? (
                <div>
                  <label htmlFor={`${id}-path`} className="text-sm">
                    Model file path
                  </label>
                  <input
                    id={`${id}-path`}
                    className={field}
                    value={path}
                    onChange={(event) => {
                      setPath(event.target.value);
                      setImportConsent(false);
                    }}
                  />
                </div>
              ) : (
                <div className="space-y-2">
                  <button type="button" className={button} onClick={() => void pickFile()}>
                    Choose model file
                  </button>
                  <p className="break-all text-sm">{path || 'No file selected'}</p>
                </div>
              )}
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={importConsent}
                  onChange={(event) => setImportConsent(event.target.checked)}
                />
                Copy this supplied file into local model storage. Its size and hash will be checked.
              </label>
              <button
                type="button"
                className={button}
                disabled={!path.trim() || !importConsent}
                onClick={() =>
                  void action('Importing model', () =>
                    localApi.importCompanyBrainModel({ source_path: path, import_confirmed: true }),
                  )
                }
              >
                Import model
              </button>
            </fieldset>
          )}
          <fieldset
            disabled={unavailable || modelStateChanged || !setup.can_prepare}
            className="space-y-3 disabled:opacity-60"
          >
            <legend className="text-sm font-medium">Prepare for this session</legend>
            <p className="text-sm">
              {setup.active_model
                ? `Preparing replaces the currently loaded model (${setup.active_model}).`
                : 'Preparing loads the pilot model locally.'}{' '}
              This does not change your saved runtime settings or startup model.
            </p>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={prepareConsent}
                onChange={(event) => setPrepareConsent(event.target.checked)}
              />
              I want to prepare this model for the current session.
            </label>
            <button
              type="button"
              className={button}
              disabled={!prepareConsent}
              onClick={() =>
                void action(
                  'Preparing model',
                  () =>
                    localApi.prepareCompanyBrainModel({
                      prepare_confirmed: true,
                      expected_active_model: setup.active_model,
                    }),
                  (result) => {
                    if (result.ready) {
                      setPrepareConsent(false);
                      onModelChange(result.model.id);
                    }
                  },
                )
              }
            >
              Prepare model
            </button>
          </fieldset>
          {setup.can_unload && (
            <button
              type="button"
              className={button}
              disabled={unavailable || modelStateChanged}
              onClick={() =>
                void action(
                  'Ending model session',
                  () =>
                    localApi.unloadCompanyBrainModel({ expected_active_model: setup.active_model }),
                  (result) => {
                    if (!result.ready && result.active_model === null) onModelChange(null);
                  },
                )
              }
            >
              End model session
            </button>
          )}
        </div>
      )}
      <button
        type="button"
        className={button}
        disabled={disabled || !!busy}
        onClick={() =>
          void action('Checking setup', () => localApi.companyBrainModelSetup(), undefined, false)
        }
      >
        Refresh setup
      </button>
    </section>
  );
}
