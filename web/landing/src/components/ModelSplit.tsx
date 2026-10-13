'use client';

import { useState } from 'react';

const MACHINES = [
  { name: 'Your laptop', kind: 'laptop', color: 'blue' },
  { name: 'Gaming PC', kind: 'tower', color: 'navy' },
  { name: 'Everyday desktop', kind: 'desktop', color: 'green' },
  { name: 'Spare laptop', kind: 'laptop', color: 'peach' },
] as const;

// Unequal visual portions explain distribution. These are not fit estimates.
const PORTIONS: Record<number, number[]> = { 2: [4, 8], 3: [3, 6, 3], 4: [2, 5, 3, 2] };

function ComputerSymbol({ kind }: { kind: string }) {
  return <svg viewBox="0 0 132 88" fill="none" aria-hidden="true">
    {kind === 'laptop' ? <><rect x="22" y="10" width="88" height="56" rx="4" /><path d="m22 66-12 12h112l-12-12M54 78h24" /><path className="split-screen" d="M31 20h70v36H31z" /></>
      : kind === 'tower' ? <><rect x="40" y="5" width="52" height="76" rx="5" /><path d="M52 17h28M52 25h28M52 33h28" /><circle cx="66" cy="65" r="5" /><path className="split-screen" d="M51 42h30v9H51z" /></>
        : <><rect x="12" y="8" width="108" height="60" rx="4" /><path d="M57 68v11m18-11v11M43 81h46" /><path className="split-screen" d="M22 18h88v40H22z" /></>}
  </svg>;
}

/** A conceptual model partition, not live discovery or a memory calculator. */
export function ModelSplit() {
  const [count, setCount] = useState(4);
  const portions = PORTIONS[count]!;

  return <div className="split-illustration home-wrap" id="split-example">
    <div className="split-diagram-heading">
      <p>One model. <strong>{count} machines.</strong></p>
      <div className="split-machine-count" role="group" aria-label="Number of machines in the illustration">
        {[2, 3, 4].map((value) => <button type="button" key={value} aria-pressed={count === value} aria-controls="split-explanation" onClick={() => setCount(value)}>{value} machines</button>)}
      </div>
    </div>
    <div className="split-model-band" aria-label={`One model divided into ${count} portions`}>
      {portions.map((size, i) => <div key={i} className={`split-model-portion split-${MACHINES[i]!.color}`} style={{ flex: size }}><span>Part {i + 1}</span><div className="split-weight-blocks" aria-hidden="true">{Array.from({ length: size }, (_, j) => <i key={j} />)}</div></div>)}
    </div>
    <svg className="split-connectors" viewBox="0 0 1000 34" preserveAspectRatio="none" fill="none" aria-hidden="true">
      {portions.map((size, i) => {
        const start = (portions.slice(0, i).reduce((sum, value) => sum + value, 0) + size / 2) / 12 * 1000;
        const end = 125 + i * 250;
        return <path key={i} d={`M${start} 0V8L${end} 26V34`} vectorEffect="non-scaling-stroke" />;
      })}
    </svg>
    <ol className="split-computers" aria-label="Machines sharing this model">
      {MACHINES.map((machine, i) => <li key={machine.name} className={`split-computer split-${machine.color}`} data-participating={i < count}>
        <span className="split-part-label">{i < count ? `Part ${i + 1} runs here` : 'Not in this setup'}</span>
        <ComputerSymbol kind={machine.kind} />
        <span className="split-computer-name">{machine.name}</span>
      </li>)}
    </ol>
    <p className="split-explanation" id="split-explanation" aria-live="polite">{count} computers hold different parts of the same model and work together on one answer.</p>
    <p className="split-example-note">Concept illustration, not a hardware estimate. Actual placement depends on the model, compatible hardware, and available memory.</p>
  </div>;
}
