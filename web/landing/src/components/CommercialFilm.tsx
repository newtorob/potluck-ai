'use client';

import { useEffect, useRef, useState } from 'react';

export function CommercialFilm() {
  const video = useRef<HTMLVideoElement>(null);
  const [showPlayButton, setShowPlayButton] = useState(false);

  // Native controls remain usable before hydration and without JavaScript.
  useEffect(() => setShowPlayButton(true), []);

  function play() {
    setShowPlayButton(false);
    video.current?.focus();
    // If playback is rejected, the native controls remain available to retry.
    void video.current?.play().catch(() => setShowPlayButton(false));
  }

  return (
    <div className="home-film-player">
      <video
        ref={video}
        controls
        playsInline
        preload="none"
        width={1920}
        height={1080}
        poster="/films/potluck-v3-5-poster.webp"
        aria-label="Potluck: Bring what you've got. Build something bigger."
        onPlay={() => setShowPlayButton(false)}
      >
        <source src="/films/potluck-v3-5.mp4" type="video/mp4" />
        <track
          kind="captions"
          src="/films/potluck-v3-5.en.vtt"
          srcLang="en"
          label="English"
        />
        <a href="/films/potluck-v3-5.mp4">Watch the Potluck film</a>.
      </video>
      {showPlayButton && (
        <button
          type="button"
          className="home-film-play"
          onClick={play}
          aria-label="Play the Potluck film, 30 seconds"
        >
          <span className="home-film-play-label">
            <svg width="16" height="18" viewBox="0 0 16 18" fill="currentColor" aria-hidden="true">
              <path d="M1 1.5v15l13-7.5z" />
            </svg>
            Play film <span>0:30</span>
          </span>
        </button>
      )}
    </div>
  );
}
