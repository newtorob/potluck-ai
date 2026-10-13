/**
 * ModelCard - "Update available" badge.
 *
 * The sidecar optionally sends `upgrade_from` on a catalog entry when it
 * supersedes something the user already has installed. The badge must only
 * render when that field is a non-empty array, and must never show a raw
 * slug - only a resolved display name or generic fallback copy.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ModelCard } from './ModelCard';
import type { CatalogModel } from '@/stores/modelStore';

const BASE_MODEL: CatalogModel = {
  slug: 'llama-3.2-3b-instruct-q4',
  name: 'Llama 3.2 3B',
  tier: 'small',
  description: 'A small, fast model.',
  size_bytes: 2_000_000_000,
  min_ram_gb: 8,
  min_vram_gb: 0,
  context_window: 8192,
  recommended_tier: 'small',
  is_featured: false,
  installed: false,
};

const noop = () => {};

function renderCard(model: CatalogModel, upgradeFromName?: string) {
  return render(
    <ModelCard
      model={model}
      isActive={false}
      isInstalling={false}
      isLoading={false}
      hardware={null}
      {...(upgradeFromName !== undefined ? { upgradeFromName } : {})}
      onInstall={noop}
      onCancelInstall={noop}
      onLoad={noop}
      onUnload={noop}
      onDelete={noop}
    />,
  );
}

describe('ModelCard: update available badge', () => {
  it('renders no update badge when upgrade_from is absent', () => {
    renderCard(BASE_MODEL);
    expect(screen.queryByText(/update available/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Newer than/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^install/i })).toBeInTheDocument();
  });

  it('renders no update badge when upgrade_from is an empty array', () => {
    renderCard({ ...BASE_MODEL, upgrade_from: [] });
    expect(screen.queryByText(/update available/i)).not.toBeInTheDocument();
  });

  it('renders a generic "Update available" badge when the superseded model name cannot be resolved', () => {
    renderCard({ ...BASE_MODEL, upgrade_from: ['some-old-slug-q4'] });
    expect(screen.getByText('Update available')).toBeInTheDocument();
    // Never show the raw slug to the user.
    expect(screen.queryByText(/some-old-slug-q4/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /update to llama 3\.2 3b/i })).toBeInTheDocument();
  });

  it('renders "Newer than <name>" when the superseded model name is resolvable', () => {
    renderCard({ ...BASE_MODEL, upgrade_from: ['llama-3.1-3b-instruct-q4'] }, 'Llama 3.1 3B');
    expect(screen.getByText('Newer than Llama 3.1 3B')).toBeInTheDocument();
  });
});
