/**
 * An errored assistant message still has an answer in it. Flipping a streamed
 * reply to 'error' must not visually delete the tokens the user already read,
 * and a 'partial' reply must not read like a finished one.
 */

import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { MessageBubble } from './MessageBubble';
import type { AppMessage } from '@/stores/chatStore';

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
