import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';

// Capture the callbacks the component registers so each test can drive the
// updater events the main process would emit over IPC.
const h = vi.hoisted(() => ({
  cbs: {} as {
    available?: (v: string) => void;
    progress?: (p: number) => void;
    downloaded?: () => void;
    error?: (m: string) => void;
  },
  downloadUpdate: vi.fn(),
  quitAndInstall: vi.fn(),
}));

vi.mock('@/lib/ipc', () => ({
  ipc: {
    onUpdateAvailable: (cb: (v: string) => void) => {
      h.cbs.available = cb;
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
    quitAndInstall: h.quitAndInstall,
  },
}));

import { UpdateBanner } from './UpdateBanner';

describe('UpdateBanner', () => {
  beforeEach(() => {
    h.cbs = {};
    h.downloadUpdate.mockReset();
    h.quitAndInstall.mockReset();
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

  it('shows progress, then a Restart action that quits-and-installs', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));

    act(() => h.cbs.progress?.(42));
    expect(screen.getByText(/42%/)).toBeInTheDocument();

    act(() => h.cbs.downloaded?.());
    fireEvent.click(screen.getByRole('button', { name: /restart to update/i }));
    expect(h.quitAndInstall).toHaveBeenCalledTimes(1);
  });

  it('surfaces a failure message on error', () => {
    render(<UpdateBanner />);
    act(() => h.cbs.available?.('1.2.3'));
    act(() => h.cbs.error?.('network down'));
    expect(screen.getByText(/failed to download/i)).toBeInTheDocument();
  });
});
