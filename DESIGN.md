---
version: alpha
name: Potluck Hearth
description: Warm, practical desktop controls for local AI and explicitly chosen network compute.
omitted:
  - section: colors
    reason: Theme values are owned by apps/desktop/frontend/src/styles/globals.css.
  - section: typography
    reason: Font stacks are owned by apps/desktop/frontend/tailwind.config.ts.
  - section: rounded
    reason: The runtime radius token and Tailwind adapter remain authoritative.
  - section: spacing
    reason: Use existing Tailwind spacing and sibling layouts; this document introduces no new scale.
  - section: components
    reason: Component owners and behavior are linked below rather than copied into a second token system.
---

# Potluck Hearth

## Overview

Potluck is a desktop tool for running local AI and choosing when to connect machines. Its product screens should feel like a well-used workroom: warm surfaces, readable controls, and clear information about what is running. The interface is English; this document makes no claim of a localized or market-specific experience.

The existing visual references are recorded in the runtime stylesheet: Things 3 for spacing, Oxide for warm charcoal and serif headings, and Mercury for restrained desktop chrome. The signature is the serif headline against quiet, practical controls. Preserve this identity when adding features; avoid promotional urgency, dashboard decoration, or a different palette for each view.

**Token ownership:** [globals.css](apps/desktop/frontend/src/styles/globals.css) owns light and dark HSL theme values, motion, and reduced-motion behavior. [tailwind.config.ts](apps/desktop/frontend/tailwind.config.ts) maps those values to semantic utilities and owns font stacks. This file documents that mapping; it does not generate tokens. Change the runtime owners and this guidance together when changing the system.

## Colors

Light mode uses warm paper; dark mode uses warm charcoal. Ember identifies primary actions. Use semantic `background`, `foreground`, `card`, `muted`, `border`, and `ring` utilities instead of literal colors. Text accents have separate contrast-aware tokens from button fills. Sage is reserved for active/online signals. A color alone must never identify status, failure, or routing scope.

## Typography

Headings inherit Instrument Serif. Inter Variable is the body and control face; JetBrains Mono Variable is for code, model identifiers, and technical values. Use the existing fallback stacks. Keep product instructions short and concrete; describe what an action does rather than how it is implemented.

## Layout

The desktop shell owns the title bar, sidebar, main view, and status bar. Views own their scrolling area. Standalone entry and account prompts use the existing centered form layout with a bounded reading width and reachable controls on a narrow viewport. Preserve the user's entered email through recoverable failures. Do not hide a local entry action behind a sign-in spinner.

## Elevation & Depth

Use surface tinting and warm hairline borders. Reuse the existing popover or dialog treatment when a layer is needed. Do not introduce decorative shadow stacks for ordinary content.

## Shapes

The runtime base radius is `0.5rem`; Tailwind derives smaller radii. Existing account controls use `rounded-xl` as their established variant. Reuse a sibling control's geometry rather than defining another radius or density for the same action.

## Components

| Workflow | Owner | Contract |
| --- | --- | --- |
| App entry | `AuthGate` and `SignInWall` | Offer explicit local access alongside passwordless sign-in. Preserve deep-link confirmation. |
| Account-backed feature | `AccountRequired` | Keep app navigation available and explain why the feature needs sign-in; do not mount its network controls first. |
| Email sign-in | `MagicLinkForm`, `VerifyingLink`, and `useAuth` | Reuse submission, error, waiting, and confirmation behavior. Backend authorization remains authoritative. |
| Local chat boundary | `localUse` and `chatStore.sendMessage` | Guard the dispatch as well as the visible selector; restored preferences must not send a signed-out local request remotely. |
| First-run model choice | `FirstRun` and existing onboarding steps | Download and load require an explicit action. Account-free entry does not enable contribution. |

The user-facing local/account distinction is documented in [Getting started](docs/install.md). Choosing local access selects Local routing and preserves chat history and installed models. Signing in later preserves that data. An already configured distributed model is not automatically stopped or relabeled as a local model.

Use semantic buttons and links, visible keyboard focus, named inputs, text errors, and perceivable busy states. Test the entry flow, offline auth, restored routing preferences, later sign-in, and a narrow viewport. Existing widgets elsewhere in the app are not certified by this document; changes should migrate the touched workflow without requiring an unrelated redesign.

## Do's and Don'ts

- Do state whether a request uses this runtime, other owned machines, a trusted circle, or the open pool.
- Do separate account-free local access from claims of fully offline operation.
- Do preserve local work when authentication fails or a member signs in later.
- Don't promise model quality, speed, anonymity, or compatibility without evidence.
- Don't silently turn on remote routing, compute contribution, or model downloads.
- Don't let a new entry path weaken server authorization or the existing update policy.
