/**
 * The 'outdated' daemon state: the sidecar answers, but it is the one from
 * before the last app update. meshStore asks main.js's mesh:daemon-status for
 * it while the socket answers, keeps `bridge` at 'reachable', and exposes the
 * answer as `serviceOutdated` for MeshServiceOutdatedBanner.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  peers: vi.fn(),
  daemonStatus: vi.fn(),
}));

vi.mock('@/lib/meshClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/meshClient')>();
  return {
    ...actual,
    isMeshBridgeAvailable: () => true,
    isMeshDaemonBridgeAvailable: () => true,
    meshClient: { ...actual.meshClient, status: mocks.status, peers: mocks.peers },
    meshDaemon: { ...actual.meshDaemon, status: mocks.daemonStatus },
  };
});

import { useMeshStore } from '@/stores/meshStore';
import { MeshSidecarUnreachableError } from '@/lib/meshClient';
import type { MeshStatus } from '@/types/mesh';

const SIDECAR_STATUS = {
  version: '0.1.0',
  mesh_status: 'running',
  node_id: null,
  household_id: null,
  mesh_ip: null,
  wg_public_key: null,
  external_endpoint: null,
  coordinator_connection: 'connected',
  coordinator_url: 'https://coordinator.trypotluck.ai',
  peers_online_count: 0,
  last_error: null,
  vpn_conflict: false,
  vpn_detail: '',
  wg_port_listening: true,
} as MeshStatus;

const OUTDATED = { platform: 'win32', state: 'outdated' as const, detail: 'service predates exe_sha256' };
const RUNNING = { platform: 'win32', state: 'running' as const };

describe('meshStore: outdated mesh service', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.status.mockReset().mockResolvedValue(SIDECAR_STATUS);
    mocks.peers.mockReset().mockResolvedValue({ peers: [] });
    mocks.daemonStatus.mockReset().mockResolvedValue(RUNNING);
    useMeshStore.getState().stop();
    useMeshStore.setState({
      status: null,
      peers: [],
      bridge: 'unknown',
      daemonStatus: null,
      serviceOutdated: null,
      lastError: null,
      _serviceProbeAt: 0,
    });
  });

  afterEach(() => {
    useMeshStore.getState().stop();
    vi.useRealTimers();
  });

  it('keeps the bridge reachable and records the outdated status', async () => {
    mocks.daemonStatus.mockResolvedValue(OUTDATED);

    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);

    const s = useMeshStore.getState();
    expect(s.bridge).toBe('reachable');
    expect(s.serviceOutdated).toEqual(OUTDATED);
    expect(s.daemonStatus).toBeNull();
  });

  it('leaves serviceOutdated empty for a current service', async () => {
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);

    expect(useMeshStore.getState().serviceOutdated).toBeNull();
    expect(mocks.daemonStatus).toHaveBeenCalledTimes(1);
  });

  it('clears the banner state once a refresh sees the service current', async () => {
    mocks.daemonStatus.mockResolvedValue(OUTDATED);
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(useMeshStore.getState().serviceOutdated).not.toBeNull();

    mocks.daemonStatus.mockResolvedValue(RUNNING);
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(useMeshStore.getState().serviceOutdated).toBeNull();
  });

  it('keeps the last answer when the probe itself fails', async () => {
    mocks.daemonStatus.mockResolvedValue(OUTDATED);
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);

    mocks.daemonStatus.mockRejectedValue(new Error('ipc failed'));
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(useMeshStore.getState().serviceOutdated).toEqual(OUTDATED);
  });

  it('asks about the version at most every 30 seconds while the 2s poll runs', async () => {
    useMeshStore.getState().start();
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.daemonStatus).toHaveBeenCalledTimes(1); // start()'s own refresh

    await vi.advanceTimersByTimeAsync(20_000); // ten status polls
    expect(mocks.status.mock.calls.length).toBeGreaterThanOrEqual(10);
    expect(mocks.daemonStatus).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(12_000);
    expect(mocks.daemonStatus).toHaveBeenCalledTimes(2);
  });

  it('drops the banner state when the sidecar stops answering', async () => {
    mocks.daemonStatus.mockResolvedValue(OUTDATED);
    await useMeshStore.getState().refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(useMeshStore.getState().serviceOutdated).not.toBeNull();

    mocks.status.mockRejectedValue(new MeshSidecarUnreachableError());
    mocks.daemonStatus.mockResolvedValue({ platform: 'win32', state: 'starting' });
    await useMeshStore.getState().refresh();

    const s = useMeshStore.getState();
    expect(s.bridge).toBe('daemon_starting');
    expect(s.serviceOutdated).toBeNull();
  });

  it('treats an outdated answer to an unreachable socket as the generic offline state', async () => {
    // The socket probe failed, then the service reported outdated a moment
    // later: a race, not something to show as a daemon state of its own.
    mocks.status.mockRejectedValue(new MeshSidecarUnreachableError());
    mocks.daemonStatus.mockResolvedValue(OUTDATED);

    await useMeshStore.getState().refresh();

    const s = useMeshStore.getState();
    expect(s.bridge).toBe('sidecar_offline');
    expect(s.serviceOutdated).toBeNull();
  });
});
