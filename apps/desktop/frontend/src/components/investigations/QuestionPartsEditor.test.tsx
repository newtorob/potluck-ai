import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { QuestionPartsEditor, questionPartsError } from './QuestionPartsEditor';

describe('QuestionPartsEditor', () => {
  it('shows reviewable text first and opens the canonical editor only on request', () => {
    const confirm = vi.fn();
    render(
      <QuestionPartsEditor
        parts={['What changed?', 'What remains?']}
        confirmed={false}
        disabled={false}
        reviewFirst
        note="Check these original words."
        onChange={vi.fn()}
        onConfirm={confirm}
        onUseWholeQuestion={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText('Part 1')).not.toBeInTheDocument();
    expect(screen.getByText('What remains?')).toBeVisible();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(confirm).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: 'Edit question parts' }));
    expect(screen.getByLabelText('Part 1')).toHaveValue('What changed?');
    fireEvent.click(screen.getByRole('button', { name: 'Done editing' }));
    expect(screen.queryByLabelText('Part 1')).not.toBeInTheDocument();
  });
  it('keeps invalid proposed text editable and prevents confirmation while busy or invalid', () => {
    render(
      <QuestionPartsEditor
        parts={['']}
        confirmed={false}
        disabled={true}
        reviewFirst
        onChange={vi.fn()}
        onConfirm={vi.fn()}
        onRestoreSuggestions={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Part 1')).toBeDisabled();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Restore suggestions' })).toBeDisabled();
  });
  it('rejects blanks, normalized duplicates, oversized parts and too many parts', () => {
    expect(questionPartsError(['What?', ' WHAT? '])).toContain('distinct');
    expect(questionPartsError([' '])).toContain('Fill');
    expect(questionPartsError(['x'.repeat(501)])).toContain('500');
    expect(questionPartsError(Array(7).fill('x'))).toContain('six');
    expect(questionPartsError(['Which rule?', 'Which exception?'])).toBe('');
  });
  it('edits and adds parts without dropping the last part', () => {
    const change = vi.fn();
    render(
      <QuestionPartsEditor
        parts={['Which rule?']}
        confirmed={false}
        disabled={false}
        onChange={change}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByRole('button', { name: 'Remove part 1' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Part 1'), { target: { value: 'Which exception?' } });
    expect(change).toHaveBeenCalledWith(['Which exception?']);
    fireEvent.click(screen.getByRole('button', { name: 'Add question part' }));
    expect(change).toHaveBeenCalledWith(['Which rule?', '']);
  });
  it('disables editing and confirmation while the request is busy', () => {
    render(
      <QuestionPartsEditor
        parts={['Which rule?']}
        confirmed={true}
        disabled={true}
        onChange={vi.fn()}
        onConfirm={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Part 1')).toBeDisabled();
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add question part' })).toBeDisabled();
  });
});
