import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  enable: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock('@/lib/meshClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/meshClient')>();
  return {
    ...actual,
    meshDaemon: { ...actual.meshDaemon, enable: mocks.enable },
  };
});

import { MeshServiceOutdatedBanner } from './MeshServiceOutdatedBanner';
import { useMeshStore } from '@/stores/meshStore';

const OUTDATED = { platform: 'linux', state: 'outdated' as const, detail: 'service predates exe_sha256' };

describe('MeshServiceOutdatedBanner', () => {
  beforeEach(() => {
    mocks.enable.mockReset();
    mocks.refresh.mockReset().mockResolvedValue(undefined);
    useMeshStore.setState({ serviceOutdated: null, refresh: mocks.refresh });
  });

  it('renders nothing while the service is current', () => {
    const { container } = render(<MeshServiceOutdatedBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says the service runs the previous version and offers one button', () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    render(<MeshServiceOutdatedBanner />);

    expect(
      screen.getByText(
        'The mesh service is still running the previous version. Restart it to finish the update.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restart mesh service' })).toBeEnabled();
    // Nothing prompts or restarts until the user presses it.
    expect(mocks.enable).not.toHaveBeenCalled();
  });

  it('on macOS the line also says a restart of the Mac does it', () => {
    useMeshStore.setState({ serviceOutdated: { ...OUTDATED, platform: 'darwin' } });
    render(<MeshServiceOutdatedBanner />);

    expect(
      screen.getByText(
        'The mesh service is still running the previous version. Restart it to finish the update, or restart your Mac.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restart mesh service' })).toBeEnabled();
  });

  it.each(['linux', 'win32'])('on %s the line has no mention of restarting the Mac', (platform) => {
    useMeshStore.setState({ serviceOutdated: { ...OUTDATED, platform } });
    render(<MeshServiceOutdatedBanner />);

    expect(
      screen.getByText(
        'The mesh service is still running the previous version. Restart it to finish the update.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/restart your Mac/i)).not.toBeInTheDocument();
  });

  it('shows the reason when the service cannot be restarted (macOS: several processes, no process)', async () => {
    useMeshStore.setState({ serviceOutdated: { ...OUTDATED, platform: 'darwin' } });
    mocks.enable.mockResolvedValue({
      platform: 'darwin',
      state: 'failed',
      detail: 'more than one mesh service process is running; restart your Mac to finish the update',
    });
    render(<MeshServiceOutdatedBanner />);

    fireEvent.click(screen.getByRole('button', { name: 'Restart mesh service' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('restart your Mac to finish the update');
  });

  it('runs the install path on click and re-reads the mesh state afterwards', async () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    mocks.enable.mockResolvedValue({ platform: 'linux', state: 'running' });
    render(<MeshServiceOutdatedBanner />);

    fireEvent.click(screen.getByRole('button', { name: 'Restart mesh service' }));

    await waitFor(() => expect(mocks.enable).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('disables the button and says so while the restart runs', async () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    let finish: (v: unknown) => void = () => {};
    mocks.enable.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    render(<MeshServiceOutdatedBanner />);

    fireEvent.click(screen.getByRole('button', { name: 'Restart mesh service' }));

    const busy = await screen.findByRole('button', { name: 'Restarting…' });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(mocks.enable).toHaveBeenCalledTimes(1);

    finish({ platform: 'linux', state: 'running' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restart mesh service' })).toBeEnabled());
  });

  it('shows why when the user cancels the authentication prompt', async () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    mocks.enable.mockResolvedValue({
      platform: 'linux',
      state: 'not_installed',
      detail: 'authentication cancelled',
    });
    render(<MeshServiceOutdatedBanner />);

    fireEvent.click(screen.getByRole('button', { name: 'Restart mesh service' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('authentication cancelled');
    expect(screen.getByRole('button', { name: 'Restart mesh service' })).toBeEnabled();
  });

  it('shows the error when the bridge call itself fails', async () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    mocks.enable.mockRejectedValue(new Error('bridge went away'));
    render(<MeshServiceOutdatedBanner />);

    fireEvent.click(screen.getByRole('button', { name: 'Restart mesh service' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('bridge went away');
  });
});
