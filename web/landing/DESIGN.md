---
name: Potluck public site
description: Expressive orange, oversized type, and a shared model made tangible.
colors:
  background: 'hsl(30 20% 98%)'
  foreground: 'hsl(0 0% 10%)'
  primary: 'hsl(15 86% 38%)'
  primary-foreground: 'hsl(0 0% 100%)'
  brand-orange: 'hsl(16 100% 65%)'
  slate: 'hsl(216 16% 39%)'
  border: 'hsl(30 5% 78%)'
typography:
  display:
    fontFamily: 'Bricolage Grotesque, system-ui, sans-serif'
    fontSize: '124px'
    fontWeight: 600
    lineHeight: 0.94
    letterSpacing: '-0.055em'
  body:
    fontFamily: 'Instrument Sans, system-ui, sans-serif'
    fontSize: '18px'
    lineHeight: 1.6
  editorial:
    fontFamily: 'Newsreader, Georgia, serif'
  code:
    fontFamily: 'Spline Sans Mono, monospace'
rounded:
  hero-action: '6px'
  illustration: '12px'
  device: '8px'
spacing:
  site-gutter: 'clamp(24px, 4vw, 56px)'
  section: 'clamp(72px, 8vw, 112px)'
---
# Potluck public site

## Overview

The homepage introduces open-source AI on a visitor's own computers. Its job is
to explain the shared-model idea and provide a clear path to install Potluck.
The creative direction is bold and expressive: an orange opening, large ink
headlines, and a model visibly divided among ordinary machines.

The signature is the interactive partition illustration. It carries the visual
weight previously given to the staged hardware image. It is explicitly a concept
preview, never a screenshot, live activity, or a model-fit calculator. The hero
separates available local AI from experimental model splitting in visible text.

Rob approved this bold direction for production after reviewing the local
preview. His prior palette preference still applies: avoid yellow/green.

Runtime tokens originate in `src/styles/globals.css`; Tailwind consumes them.
`fonts.css` loads self-hosted fonts. `site.css` owns the shell; `landing.css` owns
homepage composition. Keep public editorial pages on their existing light ground.

## Colors

Use brand orange as a broad field in the homepage header, hero, and closing
invitation. Pair it with charcoal text. White text on this orange is insufficient.
Use warm white within the illustration and below the opening. Primary orange,
charcoal, slate, and brand orange identify model portions, always with text labels.

Preserve the existing semantic colors for errors and status elsewhere. Model
parts do not indicate speed, health, or capacity. Focus on orange uses charcoal;
focus elsewhere retains the shared primary ring.

## Typography

Bricolage Grotesque owns the wordmark and marketing headings. The hero is much
larger and tighter than section headings, with two deliberate desktop lines.
The 124px desktop maximum scales using `clamp(66px, 8.6vw, 124px)`.
At 700px and below it uses `clamp(40px, 10vw, 68px)`, line-height .98, and tracking
-.05em. At 360px and below it uses 36px. Keep both phrases intact where possible.
Instrument Sans carries prose and controls; Newsreader remains editorial and
Spline Sans Mono remains code. Avoid new font downloads.

## Layout

The 1320px shell and shared gutters align navigation, headline, diagram, and
supporting sections. The homepage header joins the orange opening. The intro
pairs a short explanation with install and film actions; below 700px it stacks.
The model illustration follows directly beneath it. Source access stays visible
as a compact link, without a second full-width announcement bar.

The illustration has four device positions. Below 700px they become two columns;
text labels replace the connector paths. At small widths, machine-count controls
show numerals with full accessible names. Mobile navigation remains a non-modal
disclosure with Escape dismissal and focus restoration.

## Elevation & Depth

The illustration uses one 7px charcoal offset beneath its outline, giving the
opening a printed, tactile character. This is an intentional hero exception to
the flat content below. Ordinary sections and device tiles do not gain shadows.
The film retains its existing functional play-control shadow.

## Shapes

Hero actions use 6px corners. The illustration uses 12px corners, device frames
8px, and model portions 5px. Do not spread pill treatments across the page.

## Components

`ModelSplit` owns the partition control, defaults to three machines, and offers
2/3/4-machine buttons with pressed states. A polite live region announces the
result. Unused machines remain visible and explicitly labeled. Unequal portions
explain distribution; their proportions are not measurements.

Motion is limited to the partition transition on interaction, with no autoplay,
scroll reveals, or ambient loops. Reduced-motion preferences remove transitions.
Native links, film controls, FAQ disclosures, captions, and download attribution
remain intact. The install CTA leads to the existing platform requirements page.

## Do's and Don'ts

- Be bold in the opening and restrained in the explanation.
- Keep experiment disclosures next to the first install action and illustration.
- Preserve the measured configuration, date, and limitations in the proof section.
- Keep hardware names confined to the visitor's conceptual own-machine setup.
- Do not imply shared RAM guarantees, a speedup, or released multi-machine features.
- Do not introduce a fake live dashboard or simulated performance numbers.
- Keep focus visible, controls keyboard-operable, and touch targets usable.
