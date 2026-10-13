import { ImageResponse } from 'next/og';

export const size = { width: 32, height: 32 };
export const contentType = 'image/png';

// "P" lettermark — Inria Serif weight 300 on warm paper background.
// Echoes the header wordmark treatment (plain serif, no graphic mark).
export default function Icon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: 32,
          height: 32,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          // Warm off-white paper — matches --background light
          backgroundColor: '#F0EBE0',
          borderRadius: 4,
        }}
      >
        <span
          style={{
            fontFamily: 'Georgia, serif',
            fontSize: 22,
            fontWeight: 400,
            lineHeight: 1,
            letterSpacing: '-0.01em',
            // --foreground: 28 28% 12%
            color: '#261F18',
          }}
        >
          P
        </span>
      </div>
    ),
    { ...size }
  );
}
