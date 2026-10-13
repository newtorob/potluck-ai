'use client';

import { useState } from 'react';

const ROUTES = [
  {
    id: 'local',
    label: 'This laptop',
    title: 'A little work, right here.',
    description: 'A downloaded model answers on this laptop. Your prompt stays on this machine.',
    destination: 'This machine · local inference',
  },
  {
    id: 'desktop',
    label: 'Your desktop',
    title: 'A bigger model, in the next room.',
    description:
      'With My Machines selected, a paired desktop can answer using a model it has loaded.',
    destination: 'Your machines · over the private mesh',
  },
  {
    id: 'server',
    label: 'Your home server',
    title: 'Your desk can be anywhere.',
    description:
      'A reachable, paired home server can serve a loaded model while you work from your laptop.',
    destination: 'Your machines · over the private mesh',
  },
] as const;

/** A deliberately illustrative network, never presented as a live fleet or app screenshot. */
export function MachineIllustration() {
  const [route, setRoute] = useState<(typeof ROUTES)[number]['id']>('local');
  const active = ROUTES.find((item) => item.id === route)!;
  return (
    <figure
      className="machine-figure"
      aria-label="Interactive illustration of inference on your own machines"
    >
      <div className="machine-figure-heading">
        <span>One possible setup</span>
        <span>0{ROUTES.findIndex((item) => item.id === route) + 1} / 03</span>
      </div>
      <svg
        className={`machine-drawing route-${route}`}
        viewBox="0 0 600 430"
        fill="none"
        aria-hidden="true"
      >
        {/* The tabletop is a single quiet plane. Hardware is drawn, not stock iconography. */}
        <ellipse cx="314" cy="264" rx="263" ry="145" className="drawing-table" />
        <ellipse
          cx="314"
          cy="264"
          rx="232"
          ry="118"
          className="drawing-guide"
          strokeDasharray="2 7"
        />
        <path d="M238 305L340 360L469 286M238 305L127 244L209 197" className="drawing-wire" />
        <path
          key={route}
          d={
            route === 'desktop'
              ? 'M238 305L340 360L469 286'
              : route === 'server'
                ? 'M238 305L127 244L209 197'
                : 'M193 294L221 278L249 294'
          }
          className="drawing-current"
          pathLength="1"
        />

        {/* Home server */}
        <g className={`drawn-device ${route === 'server' ? 'device-selected' : ''}`}>
          <path d="M142 153L209 115L266 147L199 186Z" className="device-top" />
          <path d="M142 153V213L199 246V186Z" className="device-side" />
          <path d="M199 186L266 147V207L199 246Z" className="device-front" />
          <path d="M211 193L251 170M211 205L251 182M211 217L239 201" className="device-detail" />
          <circle cx="250" cy="201" r="2.5" className="device-indicator" />
          <path d="M159 161L174 170M159 169L181 182M159 177L181 190" className="device-detail" />
        </g>
        <path d="M181 133V81H119" className="drawing-leader" />
        <text x="59" y="74" className="drawing-label">
          HOME SERVER
        </text>
        <text x="59" y="91" className="drawing-sub-label">
          The always-on one
        </text>

        {/* GPU workstation */}
        <g className={`drawn-device ${route === 'desktop' ? 'device-selected' : ''}`}>
          <path d="M386 106L444 73L502 107L444 141Z" className="device-top" />
          <path d="M386 106V250L444 284V141Z" className="device-side" />
          <path d="M444 141L502 107V251L444 284Z" className="device-front" />
          <path d="M454 151L491 129M454 157L491 135" className="device-detail" />
          <ellipse
            cx="473"
            cy="185"
            rx="16"
            ry="22"
            transform="rotate(26 473 185)"
            className="device-fan"
          />
          <ellipse
            cx="473"
            cy="185"
            rx="6"
            ry="8"
            transform="rotate(26 473 185)"
            className="device-fan"
          />
          <ellipse
            cx="473"
            cy="231"
            rx="16"
            ry="22"
            transform="rotate(26 473 231)"
            className="device-fan"
          />
          <ellipse
            cx="473"
            cy="231"
            rx="6"
            ry="8"
            transform="rotate(26 473 231)"
            className="device-fan"
          />
          <path d="M399 131L430 149V233L399 215Z" className="device-window" />
          <path d="M405 179L424 190M405 188L424 199M405 197L424 208" className="device-detail" />
          <circle cx="474" cy="137" r="2.5" className="device-indicator" />
        </g>
        <path d="M454 79V46H386" className="drawing-leader" />
        <text x="313" y="31" className="drawing-label">
          GPU DESKTOP
        </text>
        <text x="313" y="48" className="drawing-sub-label">
          The heavy lifter
        </text>

        {/* Laptop, facing the reader */}
        <g className={`drawn-device ${route === 'local' ? 'device-selected' : ''}`}>
          <path
            d="M176 185Q175 180 181 183L292 247Q296 249 297 256L308 325L192 259Z"
            className="device-front"
          />
          <path d="M187 198L288 256L296 308L199 253Z" className="device-screen" />
          <path d="M132 294L248 360L308 325L192 259Z" className="device-top" />
          <path d="M132 294L248 360L308 325V331L248 368L132 302Z" className="device-side" />
          <path d="M151 293L243 345L282 323L190 271Z" className="device-keyboard" />
          <path
            d="M171 291L257 340M184 283L270 332M161 301L199 279M174 309L213 287M187 317L226 295M201 325L240 303M214 333L253 311M227 341L266 319"
            className="device-detail"
          />
          {/* Screen glyph is the Potluck bowl; no invented product UI. */}
          <path
            d="M229 251L232 264Q244 277 255 278L255 266M238 248L238 250M247 253L247 255"
            className="device-bowl"
          />
        </g>
        <path d="M160 323L100 357H54" className="drawing-leader" />
        <text x="30" y="378" className="drawing-label">
          YOUR LAPTOP
        </text>
        <text x="30" y="395" className="drawing-sub-label">
          Where you start
        </text>
        <text x="433" y="385" className="drawing-stamp">
          CONNECTED BY YOU
        </text>
      </svg>
      <div
        className="machine-route-picker"
        role="group"
        aria-label="Choose an illustrative inference destination"
      >
        {ROUTES.map((item, index) => (
          <button
            key={item.id}
            type="button"
            aria-pressed={route === item.id}
            onClick={() => setRoute(item.id)}
          >
            <span aria-hidden="true">0{index + 1}</span>
            {item.label}
          </button>
        ))}
      </div>
      <div className="machine-route-copy" aria-live="polite" aria-atomic="true">
        <p className="machine-route-title">{active.title}</p>
        <p>{active.description}</p>
        <span className="machine-route-destination">{active.destination}</span>
      </div>
      <figcaption>
        Routing illustration. Other machines must be paired, reachable, and ready to serve.
      </figcaption>
    </figure>
  );
}
