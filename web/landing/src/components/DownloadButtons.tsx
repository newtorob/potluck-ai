/**
 * DownloadButtons — the shared download CTA used on the landing hero and in
 * the §5 "get started" close. Three platform buttons (macOS primary, then
 * Windows and Linux). macOS carries a small secondary link for the Intel
 * build; Linux notes the .deb target distros. Styled with the .lm-download-* classes
 * in landing.css so it inherits the Hearth ember palette and sits next to the
 * scope switcher and mono links without a second design language.
 */

import { DOWNLOAD_LINKS, RELEASE_VERSION, LINUX_RELEASE_VERSION, WINDOWS_RELEASE_VERSION } from '@/lib/downloads';

// A single inline mark keeps the buttons visually anchored without pulling in
// an icon dependency. Purely decorative, hidden from assistive tech.
function DownloadGlyph() {
  return (
    <svg
      className="lm-download-glyph"
      width="15"
      height="15"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M8 1.5v8.5m0 0L4.5 6.5M8 10l3.5-3.5M2.5 13.5h11"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function DownloadButtons({ label }: { label?: string }) {
  return (
    <div className="lm-download">
      {label && <p className="lm-download-label">{label}</p>}
      <div className="lm-download-row">
        <div className="lm-download-group">
          <a className="lm-download-btn primary" href={DOWNLOAD_LINKS.macArm}>
            <DownloadGlyph />
            macOS
          </a>
          <span className="lm-download-alt">Apple Silicon · Intel update pending</span>
        </div>

        <div className="lm-download-group">
          <a className="lm-download-btn" href={DOWNLOAD_LINKS.windows}>
            <DownloadGlyph />
            Windows
          </a>
        </div>

        <div className="lm-download-group">
          <a className="lm-download-btn" href={DOWNLOAD_LINKS.linuxDeb}>
            <DownloadGlyph />
            Linux
          </a>
          <a className="lm-download-alt" href={DOWNLOAD_LINKS.linuxAppImage}>
            AppImage
          </a>
        </div>
      </div>
      <p className="lm-download-fine">
        Mac {RELEASE_VERSION} · Linux {LINUX_RELEASE_VERSION} · Windows {WINDOWS_RELEASE_VERSION} · free to download and run locally.
      </p>
    </div>
  );
}
