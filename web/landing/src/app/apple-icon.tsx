import { ImageResponse } from 'next/og';

export const size = { width: 180, height: 180 };
export const contentType = 'image/png';

// Apple touch icon — "P" lettermark at 180×180.
// Larger canvas allows more comfortable padding and slightly larger type.
export default function AppleIcon() {
  return new ImageResponse(
    (
      <div
        style={{
          width: 180,
          height: 180,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          // Warm off-white paper — matches --background light
          backgroundColor: '#F0EBE0',
          borderRadius: 22,
        }}
      >
        <span
          style={{
            fontFamily: 'Georgia, serif',
            fontSize: 118,
            fontWeight: 400,
            lineHeight: 1,
            letterSpacing: '-0.02em',
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
