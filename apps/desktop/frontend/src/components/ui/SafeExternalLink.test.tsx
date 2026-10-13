import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SafeExternalLink } from './SafeExternalLink';

// Manual check this test encodes (see FIX 1-3, apps/desktop/electron/main.js):
//   (a) a file:// href in memory/chat markdown must not navigate the window
//   (b) an http(s) link opens externally (via window.open, never in-window)
//   (c) a javascript:/file: href is inert - no <a>, no click handler that fires

describe('SafeExternalLink', () => {
  let openSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
  });

  afterEach(() => {
    openSpy.mockRestore();
  });

  it('renders http(s) links as real anchors and opens them via window.open only', () => {
    render(<SafeExternalLink href="https://example.com/x">click</SafeExternalLink>);
    const link = screen.getByRole('link', { name: 'click' });
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');

    fireEvent.click(link);
    expect(openSpy).toHaveBeenCalledWith('https://example.com/x', '_blank', 'noopener,noreferrer');
  });

  it('renders a file:// href as inert text, never an anchor, and never navigates', () => {
    render(<SafeExternalLink href="file:///Users/attacker/evil.html">memory link</SafeExternalLink>);
    expect(screen.queryByRole('link')).toBeNull();
    const span = screen.getByText('memory link');
    expect(span.tagName).toBe('SPAN');

    fireEvent.click(span);
    expect(openSpy).not.toHaveBeenCalled();
  });

  it('renders a javascript: href as inert text and never executes/opens it', () => {
    render(<SafeExternalLink href="javascript:alert(1)">payload</SafeExternalLink>);
    expect(screen.queryByRole('link')).toBeNull();
    fireEvent.click(screen.getByText('payload'));
    expect(openSpy).not.toHaveBeenCalled();
  });

  it('treats a missing href as inert text', () => {
    render(<SafeExternalLink>no href</SafeExternalLink>);
    expect(screen.queryByRole('link')).toBeNull();
  });
});
