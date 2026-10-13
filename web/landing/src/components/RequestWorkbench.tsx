'use client';

import { useEffect, useRef, useState } from 'react';
import { Arrow } from './Arrow';

type Scope = 'local' | 'machines';
type Phase = 'ready' | 'sending' | 'working' | 'returning' | 'done';
const EXAMPLES = [
  { label: 'Make a plan', prompt: 'Turn these weekend project ideas into a short plan.', answer: 'Start with the bookshelf. Measure the space, make a cut list, and get the wood on Saturday. Save the garden lights for next weekend.' },
  { label: 'Work through code', prompt: 'Explain what a Python list comprehension does.', answer: 'It builds a new list by applying an expression to each item. For example, [n * 2 for n in range(3)] gives you [0, 2, 4].' },
  { label: 'Tidy up notes', prompt: 'Pull the next steps out of my project notes.', answer: 'Send the revised sketch to the team, check the measurements, and choose a date for the first test. Keep the open questions with the project.' },
];

function Computer({ desktop = false }: { desktop?: boolean }) {
  return <svg width="60" height="54" viewBox="0 0 60 54" fill="none" aria-hidden="true"><rect x="8" y="5" width="44" height="31" rx="3" stroke="currentColor" strokeWidth="1.5" />{desktop ? <path d="M30 36v10m-12 0h24" stroke="currentColor" strokeWidth="1.5" /> : <path d="m8 36-6 9h56l-6-9M23 41h14" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />}<path d="m19 15 5 5-5 5m10 0h11" stroke="currentColor" strokeWidth="1.5" /></svg>;
}

export function RequestWorkbench() {
  const [scope, setScope] = useState<Scope>('machines');
  const [example, setExample] = useState(0);
  const [phase, setPhase] = useState<Phase>('ready');
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const busy = ['sending', 'working', 'returning'].includes(phase);
  const current = EXAMPLES[example]!;
  const destination = scope === 'local' ? 'this laptop' : 'your paired desktop';

  function cancel() { timers.current.forEach(clearTimeout); timers.current = []; }
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  function changeScope(next: Scope) { cancel(); setScope(next); setPhase('ready'); }
  function changeExample(next: number) { cancel(); setExample(next); setPhase('ready'); }
  function run() {
    cancel();
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { setPhase('done'); return; }
    setPhase(scope === 'local' ? 'working' : 'sending');
    timers.current = scope === 'local'
      ? [setTimeout(() => setPhase('done'), 1200)]
      : [setTimeout(() => setPhase('working'), 650), setTimeout(() => setPhase('returning'), 1500), setTimeout(() => setPhase('done'), 2200)];
  }
  const status = phase === 'ready' ? `Ready to answer on ${destination}.`
    : phase === 'sending' ? 'Request traveling to your paired desktop.'
    : phase === 'working' ? `The model on ${destination} is answering.`
    : phase === 'returning' ? 'Answer traveling back to your laptop.'
    : `Answered on ${destination}.`;

  return <div className="ws-workbench" data-scope={scope} data-phase={phase}>
    <div className="ws-workbench-toolbar">
      <span>Where should this run?</span>
      <div className="ws-scope-switch" role="group" aria-label="Choose an example routing scope">
        <button type="button" aria-pressed={scope === 'local'} onClick={() => changeScope('local')}>Local</button>
        <button type="button" aria-pressed={scope === 'machines'} onClick={() => changeScope('machines')}>My Machines</button>
      </div>
    </div>
    <div className="ws-route-stage" aria-label={scope === 'local' ? 'The request and model stay on this laptop.' : 'Your laptop sends a request to your paired desktop and receives its answer.'}>
      <div className={`ws-computer ${scope === 'local' ? 'is-serving' : ''}`}><Computer /><strong>Your laptop</strong><span>{scope === 'local' ? 'You + the model' : 'Where you work'}</span></div>
      <div className="ws-route-wire" aria-hidden="true">
        <span>{scope === 'local' ? 'Stays on this machine' : phase === 'returning' || phase === 'done' ? 'Answer comes back' : 'Encrypted connection'}</span>
        <svg viewBox="0 0 400 80" fill="none"><path className="ws-wire-rest" d="M5 40H395" /><path className="ws-wire-active" d="M5 40H395" pathLength="1" /><circle className="ws-packet" cx="5" cy="40" r="6" /><path className="ws-wire-arrow" d="m382 32 9 8-9 8" /></svg>
        <small>{scope === 'local' ? 'No remote inference' : 'Over your network'}</small>
      </div>
      <div className={`ws-computer ws-remote-computer ${scope === 'machines' ? 'is-serving' : ''}`}><Computer desktop /><strong>Your desktop</strong><span>{scope === 'machines' ? 'Where the model runs' : 'Not used for this request'}</span></div>
    </div>
    <div className="ws-demo-content">
      <div className="ws-demo-request">
        <div className="ws-example-options" role="group" aria-label="Choose an example task">{EXAMPLES.map((item, i) => <button key={item.label} type="button" aria-pressed={example === i} onClick={() => changeExample(i)}>{item.label}</button>)}</div>
        <p className="ws-demo-prompt">{current.prompt}</p>
        <button type="button" className="btn-primary ws-send" disabled={busy} onClick={run}>{busy ? 'Running example' : phase === 'done' ? 'Run again' : 'Send example'}<Arrow /></button>
        {busy && <button className="ws-skip" type="button" onClick={() => { cancel(); setPhase('done'); }}>Skip animation</button>}
      </div>
      <div className="ws-demo-answer" role="status" aria-live="polite" aria-atomic="true">
        <span className="ws-answer-label">{phase === 'done' ? 'Back with you' : 'The answer returns here'}</span>
        {phase === 'done' ? <p className="ws-answer-copy">{current.answer}</p> : <p className="ws-answer-waiting">{scope === 'machines' ? 'Same laptop. A different computer doing the work.' : 'One computer is enough to get started.'}</p>}
        <span className="ws-demo-status"><i aria-hidden="true" />{status}</span>
      </div>
    </div>
    <p className="ws-demo-disclosure">Illustrated example with a predefined answer, not live inference or a speed test. In Potluck, a paired machine must be reachable with a model loaded and serving enabled.</p>
  </div>;
}
