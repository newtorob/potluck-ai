/**
 * Covers the LAN-pairing PIN-reveal flow in JoinExistingPanel (see
 * apps/desktop/go/potluck-mesh/internal/localapi/server.go's
 * POST /pairing/reveal for the sidecar side of this contract).
 *
 * The panel does not one-click-join off a discovered LAN invite: the
 * invite only carries a fingerprint, never a redeemable token. The user
 * must enter the PIN shown on the inviter's screen; the panel exchanges it
 * for the real token via /pairing/reveal, then feeds that token into the
 * existing /pair flow.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  discoveredPairing: vi.fn(),
  revealPairing: vi.fn(),
  pair: vi.fn(),
}));

vi.mock('@/lib/meshClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/meshClient')>();
  return {
    ...actual,
    meshClient: {
      ...actual.meshClient,
      discoveredPairing: mocks.discoveredPairing,
      revealPairing: mocks.revealPairing,
      pair: mocks.pair,
    },
  };
});

import { JoinExistingPanel } from './HouseholdView';
import { MeshApiError } from '@/lib/meshClient';

describe('JoinExistingPanel: PIN reveal flow', () => {
  beforeEach(() => {
    mocks.discoveredPairing.mockReset();
    mocks.revealPairing.mockReset();
    mocks.pair.mockReset();
  });

  it('submits the PIN to /pairing/reveal, then pairs with the revealed token', async () => {
    mocks.discoveredPairing.mockResolvedValue({
      found: true,
      hostname: 'robs-mac',
      fingerprint: 'fp-123',
      pin_required: true,
    });
    mocks.revealPairing.mockResolvedValue({ token: 'real-token-abc' });
    mocks.pair.mockResolvedValue({
      node_id: 'n1',
      household_id: 'h1',
      mesh_ip: '10.0.0.2',
      mesh_cidr: '10.0.0.0/24',
      peers: [],
    });
    const onPaired = vi.fn();

    render(<JoinExistingPanel onPaired={onPaired} />);

    await screen.findByText(/found an invite/i);

    fireEvent.change(screen.getByLabelText(/confirmation pin/i), {
      target: { value: '482913' },
    });
    fireEvent.click(screen.getByRole('button', { name: /join robs-mac/i }));

    await waitFor(() => expect(mocks.revealPairing).toHaveBeenCalledWith('fp-123', '482913'));
    await waitFor(() =>
      expect(mocks.pair).toHaveBeenCalledWith({ pairing_token: 'real-token-abc' }),
    );
    await waitFor(() => expect(onPaired).toHaveBeenCalledTimes(1));
  });

  it('shows an error and does not pair when the PIN is wrong', async () => {
    mocks.discoveredPairing.mockResolvedValue({
      found: true,
      hostname: 'robs-mac',
      fingerprint: 'fp-123',
      pin_required: true,
    });
    mocks.revealPairing.mockRejectedValue(
      new MeshApiError('reveal_failed', 'incorrect pairing PIN', 401),
    );
    const onPaired = vi.fn();

    render(<JoinExistingPanel onPaired={onPaired} />);

    await screen.findByText(/found an invite/i);
    fireEvent.change(screen.getByLabelText(/confirmation pin/i), {
      target: { value: '000000' },
    });
    fireEvent.click(screen.getByRole('button', { name: /join robs-mac/i }));

    await screen.findByText(/incorrect pin/i);
    expect(mocks.pair).not.toHaveBeenCalled();
    expect(onPaired).not.toHaveBeenCalled();
  });

  it('stops accepting guesses after the 5-attempt cap and tells the user to restart', async () => {
    mocks.discoveredPairing.mockResolvedValue({
      found: true,
      hostname: 'robs-mac',
      fingerprint: 'fp-123',
      pin_required: true,
    });
    mocks.revealPairing.mockRejectedValue(
      new MeshApiError('reveal_failed', 'incorrect pairing PIN', 401),
    );
    const onPaired = vi.fn();

    render(<JoinExistingPanel onPaired={onPaired} />);
    await screen.findByText(/found an invite/i);

    const pinField = screen.getByLabelText(/confirmation pin/i);
    const joinButton = screen.getByRole('button', { name: /join robs-mac/i });

    for (let i = 0; i < 5; i++) {
      fireEvent.change(pinField, { target: { value: '000000' } });
      fireEvent.click(joinButton);
      await waitFor(() => expect(mocks.revealPairing).toHaveBeenCalledTimes(i + 1));
    }

    await screen.findByText(/too many attempts/i);
    expect(joinButton).toBeDisabled();
    expect(mocks.pair).not.toHaveBeenCalled();
    expect(onPaired).not.toHaveBeenCalled();
  });
});
