/** A concept diagram, not a screenshot, measured placement, or live status. */
export function MachineConnection({ mode = 'remote-inference' }: { mode?: 'remote-inference' | 'split-inference' }) {
  const split = mode === 'split-inference';
  return (
    <div className="machine-connection" role="img" aria-label={split ? 'Concept illustration: two computers hold different parts of one model and work together on an answer.' : 'A request goes from your laptop to your other computer. The model runs there and returns an answer.'}>
      <div className="connection-machine">
        <svg width="96" height="72" viewBox="0 0 96 72" fill="none" aria-hidden="true"><rect x="17" y="8" width="62" height="42" rx="3" /><path d="M17 50 6 61h84L79 50M37 61h22M27 21h26m-26 8h40m-40 8h17" /></svg>
        <span>{split ? 'Worker computer' : 'Your laptop'}</span><small>{split ? 'Part of the model' : 'Ask here'}</small>
      </div>
      <div className="connection-path"><span>{split ? 'One model' : 'Request'}</span><svg width="110" height="34" viewBox="0 0 110 34" fill="none" aria-hidden="true"><path d="M3 8h100m-6-6 6 6-6 6M107 26H7m6-6-6 6 6 6" /></svg><span>{split ? 'Shared work' : 'Answer'}</span></div>
      <div className="connection-machine">
        <svg width="76" height="72" viewBox="0 0 76 72" fill="none" aria-hidden="true"><rect x="16" y="5" width="44" height="62" rx="3" /><path d="M25 16h26M25 24h26M25 32h26" /><circle cx="38" cy="53" r="4" /></svg>
        <span>{split ? 'Main computer' : 'Your other computer'}</span><small>{split ? 'Rest of the model' : 'Run the model here'}</small>
      </div>
    </div>
  );
}
