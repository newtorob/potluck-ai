import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { GuidedCompanyBrainView } from './GuidedCompanyBrainView';
import { InvestigationsView } from './InvestigationsView';
import { investigationButton as button } from '@/components/investigations/controls';

export function CompanyBrainView() {
  const [params, setParams] = useSearchParams();
  const [handoff, setHandoff] = useState<{ question: string; answerId?: string }>({ question: '' });
  if (params.get('mode') === 'advanced')
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="border-b border-border px-6 py-2">
          <button className={button} onClick={() => setParams({})}>
            Back to Company Brain
          </button>
        </div>
        <div className="min-h-0 flex-1">
          <InvestigationsView
            initialQuestion={handoff.question}
            initialInvestigationId={handoff.answerId}
          />
        </div>
      </div>
    );
  return (
    <GuidedCompanyBrainView
      initialQuestion={handoff.question}
      onAdvanced={(question, answerId) => {
        setHandoff(answerId ? { question, answerId } : { question });
        setParams({ mode: 'advanced' });
      }}
    />
  );
}
