import { ImageResponse } from 'next/og';
import { readFileSync } from 'fs';
import { join } from 'path';

export const alt = 'Potluck. Good machines. Better together. Local AI, connected by you.';
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

// Load Inria Serif Light (300) TTF from public/fonts/.
// satori (the engine behind ImageResponse) requires TTF or OTF — not woff2.
// The TTF is committed to public/fonts/ to avoid a network fetch at build time.
// Source: https://fonts.gstatic.com/s/inriaserif/v18/fC14PYxPY3rXxEndZJAzN3wAVQjF.ttf
// Fallback: Georgia if the file is unavailable (shouldn't happen in normal builds).
function loadInriaSerifLight(): ArrayBuffer | null {
  try {
    const fontPath = join(process.cwd(), 'public', 'fonts', 'InriaSerif-Light.ttf');
    const buf = readFileSync(fontPath);
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
  } catch {
    return null;
  }
}

export default function OGImage() {
  const fontData = loadInriaSerifLight();

  return new ImageResponse(
    <div
      style={{
        width: 1200,
        height: 630,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'space-between',
        // Warm off-white paper — --background: 38 30% 94% in light mode
        backgroundColor: '#F0EBE0',
        padding: '72px 80px',
        fontFamily: fontData ? 'Inria Serif' : 'Georgia, serif',
      }}
    >
      {/* Top — masthead label */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <span
          style={{
            fontFamily: 'system-ui, sans-serif',
            fontSize: 13,
            fontWeight: 600,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            // --muted-foreground: 28 12% 38%
            color: '#665B50',
          }}
        >
          Local AI, connected by you
        </span>
        <span
          style={{
            width: 24,
            height: 1,
            backgroundColor: '#C4BAB0',
          }}
        />
        <span
          style={{
            fontFamily: 'system-ui, sans-serif',
            fontSize: 13,
            fontWeight: 600,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: '#665B50',
          }}
        >
          Desktop available
        </span>
      </div>

      {/* Middle — wordmark + description */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 28,
        }}
      >
        <div
          style={{
            fontFamily: fontData ? 'Inria Serif' : 'Georgia, serif',
            display: 'flex',
            flexDirection: 'column',
            fontSize: 76,
            fontWeight: 300,
            lineHeight: 1.05,
            letterSpacing: '-0.02em',
            // --foreground: 28 28% 12%
            color: '#261F18',
          }}
        >
          <span>Good machines.</span>
          <span style={{ color: '#A05030' }}>Better together.</span>
        </div>

        <div
          style={{
            fontFamily: 'system-ui, sans-serif',
            fontSize: 26,
            fontWeight: 400,
            lineHeight: 1.45,
            color: '#665B50',
            maxWidth: 760,
          }}
        >
          Run open models on the hardware you own. Connect your machines with Potluck.
        </div>
      </div>

      {/* Bottom — rule + tagline + URL */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderTop: '2px solid rgba(38, 31, 24, 0.12)',
          paddingTop: 24,
        }}
      >
        <span
          style={{
            fontFamily: fontData ? 'Inria Serif' : 'Georgia, serif',
            fontSize: 20,
            fontWeight: 300,
            // --primary: 16 55% 46% → rust/ember
            color: '#A05030',
          }}
        >
          Potluck · Everyone brings what they can.
        </span>
        <span
          style={{
            fontFamily: 'system-ui, sans-serif',
            fontSize: 16,
            fontWeight: 500,
            letterSpacing: '0.04em',
            color: '#8C7E72',
          }}
        >
          trypotluck.ai
        </span>
      </div>
    </div>,
    {
      ...size,
      fonts: fontData
        ? [
            {
              name: 'Inria Serif',
              data: fontData,
              style: 'normal',
              weight: 300,
            },
          ]
        : [],
    },
  );
}
