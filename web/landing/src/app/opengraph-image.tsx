import { ImageResponse } from 'next/og';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export const alt = 'Potluck. Bigger models. Same computers. One model split across the computers you own.';
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

export default function OGImage() {
  const display = readFileSync(join(process.cwd(), 'public/fonts/bricolage-share.ttf'));
  const body = readFileSync(join(process.cwd(), 'public/fonts/instrument-share.ttf'));
  const hardware = readFileSync(join(process.cwd(), 'public/images/potluck-computers-vivid.png')).toString('base64');
  return new ImageResponse(
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', padding: '28px 52px', background: '#fbfaf9', color: '#1a1a1a', fontFamily: 'Instrument Sans' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}><span style={{ fontFamily: 'Bricolage Grotesque', fontSize: 32, fontWeight: 600 }}>Potluck</span><span style={{ fontSize: 18 }}>Your machines, together.</span></div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 28 }}>
        <div style={{ display: 'flex', flexDirection: 'column', fontFamily: 'Bricolage Grotesque', fontSize: 68, lineHeight: 1.02, fontWeight: 600, letterSpacing: '-2.5px' }}><span>Bigger models.</span><span style={{ color: '#b43b0e' }}>Same computers.</span></div>
        <div style={{ display: 'flex', width: 280, fontSize: 24, lineHeight: 1.4 }}>One AI model, split across the computers you already own.</div>
      </div>
      {/* Local generated illustration; dimensions preserve the complete artwork. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={`data:image/png;base64,${hardware}`} alt="" width={960} height={357} style={{ alignSelf: 'center', marginTop: 0 }} />
      <div style={{ position: 'absolute', bottom: 22, left: 52, right: 52, display: 'flex', justifyContent: 'space-between', fontSize: 15 }}><span>Model splitting · experimental preview</span><span>trypotluck.ai</span></div>
    </div>,
    { ...size, fonts: [{ name: 'Bricolage Grotesque', data: display, weight: 600, style: 'normal' }, { name: 'Instrument Sans', data: body, weight: 400, style: 'normal' }] },
  );
}
