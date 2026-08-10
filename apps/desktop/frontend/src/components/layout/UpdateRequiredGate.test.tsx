import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';

const h = vi.hoisted(() => ({
  check: vi.fn(),
  download: vi.fn(),
  install: vi.fn(),
}));

vi.mock('@/lib/ipc', () => ({
  ipc: {
    hasUpdater: true,
    checkForUpdate: h.check,
    downloadUpdate: h.download,
    installUpdate: h.install,
  },
}));

import { UpdateRequiredGate } from './UpdateRequiredGate';
import { useUpdateStore } from '@/stores/updateStore';

const RESET_STATE = {
  phase: 'idle' as const,
  version: null,
  percent: 0,
  errorMessage: null,
  dismissed: false,
  minimumVersion: '0.5.0',
  latestVersion: '0.9.0',
  notesUrl: null,
  blocked: true,
  versionCheckDone: true,
};

describe('UpdateRequiredGate', () => {
  beforeEach(() => {
    h.check.mockReset();
    h.download.mockReset();
    h.install.mockReset();
    useUpdateStore.setState(RESET_STATE);
  });

  it('checks for an update on mount and shows the current version', () => {
    render(<UpdateRequiredGate />);
    expect(h.check).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/update required/i)).toBeInTheDocument();
    expect(screen.getByText(/0\.9\.0/)).toBeInTheDocument();
  });

  it('always shows the fallback download link', () => {
    render(<UpdateRequiredGate />);
    expect(screen.getByRole('link', { name: /trypotluck\.ai/i })).toBeInTheDocument();
  });

  it('"Update now" starts a download, and "Restart to update" installs once ready', () => {
    render(<UpdateRequiredGate />);
    fireEvent.click(screen.getByRole('button', { name: /update now/i }));
    expect(h.download).toHaveBeenCalledTimes(1);

    act(() => useUpdateStore.getState().setDownloaded());
    fireEvent.click(screen.getByRole('button', { name: /restart to update/i }));
    expect(h.install).toHaveBeenCalledTimes(1);
  });
});
