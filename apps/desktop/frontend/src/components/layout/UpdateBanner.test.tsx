import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';

// Capture the callbacks the component registers so each test can drive the
// updater events window.potluck.updater would emit.
const h = vi.hoisted(() => ({
  cbs: {} as {
    available?: (v: string) => void;
    notAvailable?: () => void;
    progress?: (p: number) => void;
    downloaded?: () => void;
    error?: (m: string) => void;
  },
  downloadUpdate: vi.fn(),
  installUpdate: vi.fn(),
  checkForUpdate: vi.fn(),
}));

vi.mock('@/lib/ipc', () => ({
  ipc: {
    hasUpdater: true,
    buildInfo: { version: '0.1.0', packaged: true },
    onUpdateAvailable: (cb: (v: string) => void) => {
      h.cbs.available = cb;
      return () => {};
    },
    onUpdateNotAvailable: (cb: () => void) => {
      h.cbs.notAvailable = cb;
      return () => {};
    },
    onUpdateProgress: (cb: (p: number) => void) => {
      h.cbs.progress = cb;
      return () => {};
    },
    onUpdateDownloaded: (cb: () => void) => {
      h.cbs.downloaded = cb;
      return () => {};
    },
    onUpdateError: (cb: (m: string) => void) => {
      h.cbs.error = cb;
      return () => {};
    },
    downloadUpdate: h.downloadUpdate,
    installUpdate: h.installUpdate,
    checkForUpdate: h.checkForUpdate,
  },
}));

import { UpdateBanner } from './UpdateBanner';
import { useUpdateStore } from '@/stores/updateStore';

const RESET_STATE = {
  phase: 'idle' as const,
  version: null,
  percent: 0,
  errorMessage: null,
  dismissed: false,
  minimumVersion: null,
  latestVersion: null,
  notesUrl: null,
  blocked: false,
  versionCheckDone: false,
};

describe('UpdateBanner', () => {
  beforeEach(() => {
    h.cbs = {};
    h.downloadUpdate.mockReset();
    h.installUpdate.mockReset();
    h.checkForUpdate.mockReset();
    useUpdateStore.setState(RESET_STATE);
  });

  it('checks for an update on mount when packaged and the updater bridge is present', () => {
    render(<UpdateBanner />);
    expect(h.checkForUpdate).toHaveBeenCalledTimes(1);
  });

  it('renders nothing until an update is available', () => {
    const { container } = render(<UpdateBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('offers Download when an update is available, and invokes the IPC on click', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));

    expect(screen.getByText(/1\.2\.3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /download/i }));
    expect(h.downloadUpdate).toHaveBeenCalledTimes(1);
  });

  it('shows progress, then a Restart action that installs the update', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));

    act(() => h.cbs.progress?.(42));
    expect(screen.getByText(/42%/)).toBeInTheDocument();

    act(() => h.cbs.downloaded?.());
    fireEvent.click(screen.getByRole('button', { name: /restart to update/i }));
    expect(h.installUpdate).toHaveBeenCalledTimes(1);
  });

  it('surfaces a failure message on error with a Retry action', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));
    act(() => h.cbs.error?.('network down'));
    expect(screen.getByText(/failed to download/i)).toBeInTheDocument();
    expect(screen.getByText(/network down/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(h.downloadUpdate).toHaveBeenCalledTimes(1);
  });

  it('dismisses the available banner', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/1\.2\.3/)).not.toBeInTheDocument();
  });
});
