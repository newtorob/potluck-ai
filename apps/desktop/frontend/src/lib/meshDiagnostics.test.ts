import { describe, expect, it } from 'vitest';
import { deriveDiagnostics, getConnectedPeers, type DiagnosticsInput } from './meshDiagnostics';
import type { MeshPeer, MeshStatus } from '@/types/mesh';

const status: MeshStatus = {
  version: '1', mesh_status: 'running', node_id: 'self', household_id: 'home',
  mesh_ip: '100.64.0.1', wg_public_key: 'public', external_endpoint: null,
  coordinator_connection: 'connected', coordinator_url: 'https://example.com',
  peers_online_count: 3, last_error: null, vpn_conflict: false, vpn_detail: '', wg_port_listening: true,
};
const peer: MeshPeer = {
  node_id: 'peer', node_name: 'Desktop', mesh_ip: '100.64.0.2', wg_public_key: 'public-2',
  last_endpoint: null, last_seen_at: null, platform: 'linux', reachable: false, reachability_mode: null,
};
const input: DiagnosticsInput = { bridge: 'reachable', status, peers: [peer] };

describe('household connection truth', () => {
  it('does not treat coordinator presence as a working connection', () => {
    expect(getConnectedPeers(input)).toEqual([]);
    expect(deriveDiagnostics(input).headline).toBe('1 machine has no tunnel');
  });

  it('keeps a working relayed tunnel usable during a coordinator outage', () => {
    const state: DiagnosticsInput = { ...input, status: { ...status, coordinator_connection: 'disconnected' },
      peers: [{ ...peer, reachable: true, reachability_mode: 'derp' }] };
    expect(getConnectedPeers(state)).toHaveLength(1);
    expect(deriveDiagnostics(state).perPeer.peer?.state).toBe('relayed');
  });

  it('rejects cached reachable peers after the local service stops or disappears', () => {
    const peers = [{ ...peer, reachable: true }];
    expect(getConnectedPeers({ ...input, peers, bridge: 'sidecar_offline' })).toEqual([]);
    expect(getConnectedPeers({ ...input, peers, status: { ...status, mesh_status: 'stopped' } })).toEqual([]);
  });

  it('never reports healthy while keys need repair or a tunnel is still forming', () => {
    expect(deriveDiagnostics({ ...input, peers: [{ ...peer, key_configured: false }] }).headline).toContain('key refresh');
    const now = Date.now();
    expect(deriveDiagnostics({ ...input, peers: [{ ...peer, last_seen_at: new Date(now).toISOString() }] }, now).headline).toContain('Connecting');
  });
});
