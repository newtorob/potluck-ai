/**
 * Shared release + community constants for the landing site.
 *
 * Single source of truth for the published desktop download URLs (hosted on
 * R2 at releases.trypotluck.ai) and the Discord invite. Spaces in the R2 file
 * names are percent-encoded so the links work verbatim in href attributes.
 * Bump the version here when a new build ships and every surface updates.
 */

export const RELEASE_VERSION = '0.1.11';
export const LINUX_RELEASE_VERSION = '0.1.11';
export const WINDOWS_RELEASE_VERSION = '0.1.5';

export const DOWNLOADS = {
  macArm: 'https://releases.trypotluck.ai/Potluck%20AI-0.1.11-arm64.dmg',
  // Held until native Intel sidecars and dependencies are validated.
  macIntel: null,
  windows: 'https://releases.trypotluck.ai/Potluck%20AI%20Setup%200.1.5.exe',
  // Linux supports x86-64. Windows stays on its validated 0.1.5 GPU build
  // until the native Windows build machine is available.
  linuxDeb: 'https://releases.trypotluck.ai/potluck-ai-desktop_0.1.11_amd64.deb',
  linuxAppImage: 'https://releases.trypotluck.ai/Potluck%20AI-0.1.11.AppImage',
} as const;

/**
 * Counted download links. Same keys as DOWNLOADS, but routed through
 * /dl/<slug> so each click logs an anonymous platform + timestamp row before
 * redirecting to R2 (src/app/dl/[platform]/route.ts). UI components should
 * link these; DOWNLOADS keeps the raw R2 URLs as the source of truth.
 */
export const DOWNLOAD_LINKS: Record<keyof typeof DOWNLOADS, string> = {
  macArm: '/dl/mac-arm',
  macIntel: '/dl/mac-intel',
  windows: '/dl/windows',
  linuxDeb: '/dl/linux-deb',
  linuxAppImage: '/dl/linux-appimage',
};

export const CHECKSUMS = {
  current: 'https://releases.trypotluck.ai/SHA256SUMS-0.1.11',
  linux: 'https://releases.trypotluck.ai/SHA256SUMS-0.1.11-linux',
  windows: 'https://releases.trypotluck.ai/SHA256SUMS-0.1.5-windows',
} as const;

export const DISCORD_INVITE = 'https://discord.gg/gbqTeZ2rh';

export const X_URL = 'https://x.com/potluckAI';
