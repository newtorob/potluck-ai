import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Investigation } from '@/types/investigations';
import { InvestigationOutcomeForm } from './InvestigationOutcomeForm';

const item = {
  id: 'i1',
  workspace: 'w',
  collection_ids: ['c'],
  status: 'reviewed',
  review: { decision: 'accept', reviewer: 'A', reason: 'checked' },
  outcome: null,
} as Investigation;

function fill() {
  fireEvent.change(screen.getByLabelText('Outcome'), { target: { value: 'unresolved' } });
  fireEvent.change(screen.getByLabelText('Reviewer name'), { target: { value: ' Alex ' } });
  fireEvent.change(screen.getByLabelText('Outcome notes'), {
    target: { value: ' Still pending ' },
  });
}

describe('InvestigationOutcomeForm', () => {
  it('requires review, explicit fields and saves the exact outcome', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const view = render(
      <InvestigationOutcomeForm investigation={{ ...item, review: null }} onSave={onSave} />,
    );
    expect(screen.getByText(/Review this answer first/)).toBeVisible();
    view.rerender(<InvestigationOutcomeForm investigation={item} onSave={onSave} />);
    expect(screen.getByLabelText('Outcome')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Save outcome' }));
    expect(screen.getByLabelText('Outcome')).toHaveFocus();
    expect(screen.getByLabelText('Outcome')).toHaveAttribute('aria-invalid', 'true');
    expect(onSave).not.toHaveBeenCalled();
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Save outcome' }));
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        result: 'unresolved',
        reviewer: 'Alex',
        notes: 'Still pending',
      }),
    );
    view.rerender(
      <InvestigationOutcomeForm
        investigation={{
          ...item,
          outcome: { result: 'unresolved', reviewer: 'Alex', notes: 'Still pending' },
        }}
        onSave={onSave}
      />,
    );
    expect(screen.getByText('Recorded outcome: unresolved')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Save outcome' })).not.toBeInTheDocument();
  });

  it('keeps entries on failure and permits a deliberate retry', async () => {
    const onSave = vi
      .fn()
      .mockRejectedValueOnce(new Error('Save failed'))
      .mockResolvedValueOnce(undefined);
    render(<InvestigationOutcomeForm investigation={item} onSave={onSave} />);
    fill();
    fireEvent.click(screen.getByRole('button', { name: 'Save outcome' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Save failed');
    expect(screen.getByLabelText('Outcome notes')).toHaveValue(' Still pending ');
    fireEvent.click(screen.getByRole('button', { name: 'Save outcome' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
  });

  it('locks duplicate submission and ignores a late failure for another scope', async () => {
    let reject!: (error: Error) => void;
    const onSave = vi.fn().mockImplementation(
      () =>
        new Promise<void>((_, fail) => {
          reject = fail;
        }),
    );
    const view = render(<InvestigationOutcomeForm investigation={item} onSave={onSave} />);
    fill();
    const form = screen.getByRole('button', { name: 'Save outcome' }).closest('form')!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(onSave).toHaveBeenCalledTimes(1);
    view.rerender(
      <InvestigationOutcomeForm investigation={{ ...item, workspace: 'other' }} onSave={onSave} />,
    );
    reject(new Error('Old failure'));
    await waitFor(() => expect(screen.getByLabelText('Outcome')).toHaveValue(''));
    expect(screen.queryByText('Old failure')).not.toBeInTheDocument();
  });
});

it('keeps malformed scope metadata read-only rather than crashing or dispatching', () => {
  const onSave = vi.fn();
  render(
    <InvestigationOutcomeForm
      investigation={{ ...item, collection_ids: undefined } as unknown as Investigation}
      onSave={onSave}
    />,
  );
  expect(screen.getByText(/original workspace and folders/)).toBeVisible();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  expect(onSave).not.toHaveBeenCalled();
});
it('does not dispatch a second save after success before the refreshed record arrives', async () => {
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<InvestigationOutcomeForm investigation={item} onSave={onSave} />);
  fill();
  const form = screen.getByRole('button', { name: 'Save outcome' }).closest('form')!;
  fireEvent.submit(form);
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Save outcome' })).not.toHaveAttribute(
      'aria-busy',
      'true',
    ),
  );
  fireEvent.submit(form);
  expect(onSave).toHaveBeenCalledTimes(1);
});
