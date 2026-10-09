/**
 * Where the 'outdated' mesh service state shows up in My Machines: a banner
 * above the normal view while the mesh works, never in place of it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const mocks = vi.hoisted(() => ({
  discoveredPairing: vi.fn(),
}));

vi.mock('@/lib/meshClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/meshClient')>();
  return {
    ...actual,
    isMeshBridgeAvailable: () => true,
    isMeshDaemonBridgeAvailable: () => false,
    meshClient: { ...actual.meshClient, discoveredPairing: mocks.discoveredPairing },
  };
});

import { HouseholdView } from './HouseholdView';
import { useMeshStore } from '@/stores/meshStore';
import type { MeshStatus } from '@/types/mesh';

const UNPAIRED = {
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

const OUTDATED = { platform: 'darwin', state: 'outdated' as const };
const BANNER = /still running the previous version/i;

function renderView() {
  return render(
    <MemoryRouter>
      <HouseholdView />
    </MemoryRouter>,
  );
}

describe('HouseholdView: outdated mesh service', () => {
  beforeEach(() => {
    mocks.discoveredPairing.mockReset().mockResolvedValue({ found: false });
    // isStarted keeps the view from starting the real polling timers.
    useMeshStore.setState({
      status: UNPAIRED,
      peers: [],
      bridge: 'reachable',
      daemonStatus: null,
      serviceOutdated: null,
      lastError: null,
      isStarted: true,
    });
  });

  it('shows the restart banner above the normal view when the service is outdated', () => {
    useMeshStore.setState({ serviceOutdated: OUTDATED });
    renderView();

    expect(screen.getByText(BANNER)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restart mesh service' })).toBeInTheDocument();
    // The mesh works, so the rest of the page is still there.
    expect(screen.getByRole('heading', { name: 'My Machines' })).toBeInTheDocument();
  });

  it('shows no banner when the service is current', () => {
    renderView();

    expect(screen.queryByText(BANNER)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'My Machines' })).toBeInTheDocument();
  });
});
