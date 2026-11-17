import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { CompanyBrainView } from './CompanyBrainView';
vi.mock('./GuidedCompanyBrainView', () => ({
  GuidedCompanyBrainView: ({
    onAdvanced,
    initialQuestion,
    reviewedEvidenceVersion,
    reviewedAnswerVersion,
  }: {
    onAdvanced: (q: string, id: string) => void;
    initialQuestion: string;
    reviewedEvidenceVersion: 'v5' | 'v6';
    reviewedAnswerVersion: 'v32' | 'v33';
  }) => (
    <div>
      <p>Guided question: {initialQuestion}</p>
      <p>Evidence version: {reviewedEvidenceVersion}</p>
      <p>Answer version: {reviewedAnswerVersion}</p>
      <button onClick={() => onAdvanced('Keep my question', 'saved-answer')}>
        Open review tools
      </button>
    </div>
  ),
}));
vi.mock('./InvestigationsView', () => ({
  InvestigationsView: ({
    initialQuestion,
    initialInvestigationId,
  }: {
    initialQuestion: string;
    initialInvestigationId: string;
  }) => (
    <p>
      Advanced record: {initialInvestigationId} · {initialQuestion}
    </p>
  ),
}));
it('hands a saved record to advanced review and keeps the question on return without persisting confirmation', () => {
  render(
    <MemoryRouter>
      <CompanyBrainView />
    </MemoryRouter>,
  );
  expect(screen.getByText('Evidence version: v6')).toBeVisible();
  expect(screen.getByText('Answer version: v33')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Open review tools' }));
  expect(screen.getByText('Advanced record: saved-answer · Keep my question')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Back to Company Brain' }));
  expect(screen.getByText('Guided question: Keep my question')).toBeVisible();
});
it('keeps the legacy evidence version available through an explicit caller override', () => {
  render(
    <MemoryRouter>
      <CompanyBrainView reviewedEvidenceVersion="v5" reviewedAnswerVersion="v32" />
    </MemoryRouter>,
  );
  expect(screen.getByText('Evidence version: v5')).toBeVisible();
  expect(screen.getByText('Answer version: v32')).toBeVisible();
});
