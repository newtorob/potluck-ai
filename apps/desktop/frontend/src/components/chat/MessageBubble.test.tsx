/**
 * An errored assistant message still has an answer in it. Flipping a streamed
 * reply to 'error' must not visually delete the tokens the user already read,
 * and a 'partial' reply must not read like a finished one.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageBubble } from './MessageBubble';
import { useChatStore, type AppMessage } from '@/stores/chatStore';

const restartAction = useChatStore.getState().restartMessage;
afterEach(() => useChatStore.setState({ isStreaming: false, restartMessage: restartAction }));

function message(overrides: Partial<AppMessage> = {}): AppMessage {
  return {
    id: 'm1',
    role: 'assistant',
    content: '',
    createdAt: Date.now(),
    status: 'complete',
    ...overrides,
  };
}

function renderBubble(msg: AppMessage) {
  return render(
    <MemoryRouter>
      <MessageBubble message={msg} />
    </MemoryRouter>,
  );
}

describe('MessageBubble', () => {
  it('keeps the streamed answer visible on an errored message', () => {
    renderBubble(
      message({
        status: 'error',
        content: 'The first half of the answer.',
        errorText: 'The contributor timed out before finishing.',
      }),
    );

    expect(screen.getByText('The first half of the answer.')).toBeTruthy();
    expect(screen.getByText('The contributor timed out before finishing.')).toBeTruthy();
  });

  it('falls back to a plain unfinished-reply line for a hydrated error', () => {
    renderBubble(message({ status: 'error', content: 'partial text' }));

    expect(screen.getByText('This reply did not finish.')).toBeTruthy();
    expect(screen.getByText('partial text')).toBeTruthy();
  });

  it('keeps the load-a-model affordance on a no-model error', () => {
    renderBubble(
      message({ status: 'error', errorText: '[localApi] 503: No model is loaded' }),
    );

    expect(screen.getByRole('button', { name: 'Load a model' })).toBeTruthy();
  });

  it('marks a stopped reply so it does not read as complete', () => {
    renderBubble(message({ status: 'partial', content: 'half an answer' }));

    expect(screen.getByText('Stopped early')).toBeTruthy();
  });

  it('adds no stopped marker to a complete reply', () => {
    renderBubble(message({ status: 'complete', content: 'a whole answer' }));

    expect(screen.queryByText('Stopped early')).toBeNull();
  });
});


describe('route receipt', () => {
  function openReceipt(label: RegExp): string {
    fireEvent.click(screen.getByRole('button', { name: label }));
    return screen.getByRole('region', { name: 'Privacy receipt detail' }).textContent ?? '';
  }

  it('names a trusted circle answer as the circle, never the open pool', () => {
    renderBubble(message({ content: 'hi', meta: { served_by: 'contributor', served_scope: 'circle' } }));

    expect(screen.getByText('Trusted circle')).toBeTruthy();
    expect(screen.queryByText('Open pool')).toBeNull();
    const detail = openReceipt(/Trusted circle/);
    expect(detail).toContain('Answered by your trusted circle');
    expect(detail).toContain('only to machines in the circle you chose');
    expect(detail).toContain('saw your request text');
    expect(detail).not.toContain('do not know it came from you');
  });

  describe('circle receipt wording', () => {
    const ANSWERED =
      'A machine owned by a member of your circle answered this. It processed your prompt to generate the reply, so that machine saw your request text.';
    const RELAYED = `${ANSWERED} The Potluck coordinator relayed it over encrypted connections, only to machines in the circle you chose.`;
    const TITLE = 'Answered by your trusted circle';
    const circle = (extra: Partial<NonNullable<AppMessage['meta']>>): AppMessage =>
      message({ content: 'hi', meta: { served_by: 'contributor', served_scope: 'circle', ...extra } });

    /** The title and body paragraphs of the opened receipt, exactly as shown. */
    function receipt(msg: AppMessage): { title: string | null; body: string | null } {
      renderBubble(msg);
      fireEvent.click(screen.getByRole('button', { name: /Trusted circle/ }));
      const [title, body] = screen.getByRole('region', { name: 'Privacy receipt detail' }).querySelectorAll('p');
      return { title: title?.textContent ?? null, body: body?.textContent ?? null };
    }

    it('reads as the relay when the message has no path, as saved before direct links', () => {
      expect(receipt(circle({}))).toEqual({ title: TITLE, body: RELAYED });
    });

    it('reads as the relay when the path is relay, whatever the transport field says', () => {
      expect(receipt(circle({ path: 'relay', transport: 'udp' }))).toEqual({ title: TITLE, body: RELAYED });
    });

    it('says the request went straight over an encrypted link for direct over udp', () => {
      expect(receipt(circle({ path: 'direct', transport: 'udp' }))).toEqual({
        title: TITLE,
        body: `${ANSWERED} Your machine sent it straight to theirs over an encrypted link. The Potluck coordinator set up the link and did not carry your prompt.`,
      });
    });

    it('says the link passed through the relay for direct over derp', () => {
      expect(receipt(circle({ path: 'direct', transport: 'derp' }))).toEqual({
        title: TITLE,
        body: `${ANSWERED} Your machine sent it to theirs over an encrypted link that passed through the Potluck relay, which moves the encrypted packets and cannot read them. The Potluck coordinator set up the link and did not carry your prompt.`,
      });
    });

    it('says only that the link was encrypted for direct with no transport', () => {
      expect(receipt(circle({ path: 'direct' }))).toEqual({
        title: TITLE,
        body: `${ANSWERED} Your machine sent it to theirs over an encrypted link. The Potluck coordinator set up the link and did not carry your prompt.`,
      });
    });

    it('keeps the same badge for every route', () => {
      renderBubble(circle({ path: 'direct', transport: 'udp' }));
      expect(screen.getByText('Trusted circle')).toBeTruthy();
    });
  });

  it('keeps the open pool receipt for an open pool answer', () => {
    renderBubble(message({ content: 'hi', meta: { served_by: 'contributor', served_scope: 'pool' } }));

    expect(openReceipt(/Open pool/)).toContain('Answered by the open pool');
  });

  it('claims nothing about anonymity when the tier was not saved', () => {
    // Reloaded history keeps served_by but not the scope.
    renderBubble(message({ content: 'hi', meta: { served_by: 'contributor' } }));

    expect(screen.queryByText('Open pool')).toBeNull();
    const detail = openReceipt(/Another machine/);
    expect(detail).toContain('saw your request text');
    expect(detail).not.toContain('do not know it came from you');
  });
});

describe('restart control', () => {
  const recovery = { messages: [], modelSlug: 'same-model', excludedPeerIds: ['p1'],
    routingMode: 'capability' as const, historyAllowed: false };

  it('explains the scope and restarts the interrupted message', () => {
    const restart = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ isStreaming: false, restartMessage: restart });
    renderBubble(message({ status: 'error', content: 'partial answer', recovery }));
    expect(screen.getByText(/This answer is incomplete/)).toBeTruthy();
    expect(screen.getByText(/another of your machines/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Restart on another computer' }));
    expect(restart).toHaveBeenCalledWith('m1');
  });

  it('disables restart while another request is running', () => {
    useChatStore.setState({ isStreaming: true });
    renderBubble(message({ status: 'error', recovery }));
    expect(screen.getByRole('button', { name: 'Restart on another computer' })).toBeDisabled();
  });

  it('labels the replacement as a restarted answer', () => {
    renderBubble(message({ content: 'replacement', restartOf: 'original' }));
    expect(screen.getByText('Restarted answer · My Machines')).toBeTruthy();
  });
});
