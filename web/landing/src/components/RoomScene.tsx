'use client';

import Image from 'next/image';
import { useState } from 'react';

/** An illustration of the two released routing scopes, never a live device picker. */
export function RoomScene() {
  const [scope, setScope] = useState<'local' | 'machines'>('machines');
  const remote = scope === 'machines';

  return (
    <div className="room-example" data-scope={scope}>
      <div className="room-panorama">
        <Image
          src="/images/potluck-other-room.png"
          alt="An illustrated home: a laptop on the dining table, and a desktop computer in the adjoining study."
          width={1860}
          height={845}
          priority
          fetchPriority="high"
          quality={85}
          sizes="100vw"
        />
        <div className="room-pin room-pin-laptop" aria-hidden="true"><span className={!remote ? 'is-processing' : ''}>{remote ? 'You work here' : 'You work + run AI here'}</span></div>
        <div className="room-pin room-pin-desktop" aria-hidden="true"><span className={remote ? 'is-processing' : ''}>{remote ? 'The model runs here' : 'Your other computer'}</span></div>
      </div>
      <div className="room-example-controls home-wrap">
        <div className="room-scope-buttons" role="group" aria-label="Choose where the example model runs">
          <button type="button" aria-pressed={!remote} aria-controls="room-explanation" onClick={() => setScope('local')}><span>Local</span><small>One computer</small></button>
          <button type="button" aria-pressed={remote} aria-controls="room-explanation" onClick={() => setScope('machines')}><span>My Machines</span><small>Your computers, connected</small></button>
        </div>
        <p id="room-explanation" className="room-scope-description" aria-live="polite">{remote ? 'Your laptop sends the request. An eligible paired computer runs the model and sends the answer back.' : 'The model runs on this computer. After sign-in and model setup, local inference can keep working offline.'}</p>
        <p className="room-example-note">Illustrated setup. {remote ? 'Both computers must be reachable, with the requested model loaded and serving enabled on the other machine.' : 'Choose a model that fits the memory and hardware available on this computer.'}</p>
      </div>
    </div>
  );
}
