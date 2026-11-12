# Desktop entry and account access

This contract covers entry, desktop chat routing, and the sign-in boundary. Other desktop workflows retain their existing contracts and are outside this focused audit. Visual owners are recorded in [DESIGN.md](DESIGN.md); user instructions are in [Getting started](docs/install.md).

Settings loading and recovery are covered below as a separate focused workflow.

## Reviewed-answer reading

`components/investigations/ReviewedPassageAnswer.tsx` owns reviewed-answer display
using existing Markdown, safe links, Hearth tokens and native disclosures. The
sidecar's `investigation_readable_presentation` supplies a deterministic V32 view:
shared text retains its evidence-group scope, source table labels require exact
support, and the original answer/citations stay available. A display projection
must match the current text, status and snippet IDs; corrections take precedence.
Source freshness and authority remain server-owned. See `docs/company-brain-pilot.md`.
Component and service tests cover shared conditions, source ambiguity, escaped
pipes, corrections and original-output access. No new tokens or navigation flow.

The existing Investigations pilot also owns the opt-in question-parts editor.
One to six distinct parts require explicit confirmation bound to the current
question, parts, model and collection context. Edits and request errors clear
confirmation; entered parts survive recoverable errors. Native fieldsets and the
shared investigation controls lock during dispatch; a ref guard rejects duplicate
clicks before React commits the busy state. The server retains final validation,
source freshness, part IDs and model identity authority.

## Product rules and evidence

| Rule | Source |
| --- | --- |
| Local chat, model management and local memory can be used without an account. | `docs/install.md`, `AuthGate`, `localUse` |
| Pairing, trusted circles, network controls, administration and new inference-gateway setup require account access. | `App.tsx`, `AccountRequired`, `GatewayPanel`; backend authorization remains authoritative. |
| A local entry choice does not enable contribution or stop existing background services. | `ConnectStep`, `GatewayPanel`, `docs/install.md` |
| Email verification requires explicit account confirmation before committing a session. | `useAuth`, `useDeepLinkAuth`, `SignInConfirmDialog` |
| Local history and model installations survive later sign-in. | `localUse.test.tsx`, local sidecar persistence |

Component paths below are relative to `apps/desktop/frontend/src/`.

## Canonical UI Map

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
| --- | --- | --- | --- | --- |
| Select/Listbox | Native loaded-model selectors in `PilotKnowledge` and `InvestigationsView` | Loaded sidecar model IDs and explicit selection | Native OS popup; no custom geometry or autocomplete | Pilot component tests and investigations browser checks |
| Form | `components/auth/MagicLinkForm.tsx` | `hooks/useAuth.ts` and passwordless confirmation flow | Entry form without input autofocus; dedicated sign-in form with autofocus | AuthGate and localUse tests; browser error/keyboard checks |
| Account entry | `components/auth/AuthGate.tsx`, `SignInWall.tsx` | Explicit local choice or confirmed/cached account | Account lookup, local entry, signed-in and cached offline access | `components/auth/AuthGate.test.tsx` |
| Account-backed feature | `components/auth/AccountRequired.tsx` | `stores/authStore.ts` | Full feature or in-shell sign-in prompt | `lib/localUse.test.tsx` |
| Desktop routing | `views/ChatView.tsx`, `stores/chatStore.ts` | Explicit scope and local-use preference, checked again at dispatch | Local, owned machines, trusted circle, cooperative scope subject to availability | Routing, recovery and gateway tests |
| Durable local preference | `stores/uiPrefsStore.ts`, `lib/durablePrefs.ts` | Latest explicit choice wins over in-flight hydration | Restored true/false state, new local or remote choice | `lib/localUse.test.tsx` |
| Settings recovery | `views/SettingsView.tsx`, `hooks/useSettings.ts`, `stores/settingsStore.ts` | GET `/settings`; existing `EmptyState` and Hearth control styles | Initial loading, load failure, cached refresh failure, retry, loaded settings | Settings store and view tests; isolated browser recovery checks |

## State and recovery

- Entry offers **Use on this machine** while an account lookup is pending. Choosing it selects Local and preserves history, models and other settings.
- A late account response or settings response must not change that choice. Effective chat scope and the status display agree with the dispatch guard.
- A previously loaded distributed model is preserved. Local chat explains how to load a model on this machine; a signed-in member may explicitly choose **Use this model** to accept its existing remote configuration.
- A failed runtime check prevents the local chat request and gives a recovery action. It never silently falls back to another machine.
- Signing in keeps local work. Invalid or pending sign-in does not mount account-backed controls. Existing cached offline accounts retain their established behavior.
- Account-required prompts keep shell navigation and a path back to local chat. The inference-gateway panel still permits disabling a previously configured service while signed out.
- Sign-in errors stay inline, retain the entered email, expose an alert, and leave local entry available. Duplicate submissions are disabled while pending.

## Verification boundary

Use component/store tests for delayed hydration, auth transitions, preserved history, actual dispatch guards and gateway setup restrictions. Use the isolated actual-component browser preview for keyboard focus, sign-in failure, local entry, remote-model guidance, and narrow light/dark layouts. Mocked UI previews are not live-inference or native-installer certification.

The existing universal minimum-version gate and server authorization policies remain in force. Local entry is a desktop-chat choice, not a promise that every app activity or previously configured service is offline.

## Settings loading and recovery

- Hardware, Privacy, Vault, Connect tools and Advanced share one loading state and retry action. Account remains available without loaded settings.
- An initial request failure replaces the loading skeleton with plain-language guidance and **Retry**. Raw network errors, backend addresses and internal service names are not product instructions.
- Retry keeps the error visible, exposes **Trying again…**, disables duplicate activation and preserves button dimensions. Success restores the selected panel in place.
- A failed refresh keeps previously loaded settings visible and identifies them as the last loaded values. Retrying does not reset the current tab or discard cached settings.
- Concurrent loads share one request; older responses cannot replace newer saved or hydrated settings. GET recovery does not change the existing save/rollback or authorization contract.
- Tabs retain their URL selection and arrow-key behavior. The tab strip scrolls at narrow widths; controls and error guidance remain reachable in light and dark themes.
- Browser evidence uses real components with simulated API responses and synthetic settings. It does not certify live inference, a native installer or a real service outage.


## Local investigations

`views/InvestigationsView.tsx` uses the shared `ViewHeader` and Hearth tokens.
The workflow is single-owner and local; workspace strings and typed reviewer
names are not company authentication. Its lifecycle is defined in the Python
`COMPANY_BRAIN.md` guide and enforced by the sidecar, including current evidence
validation on every read and write.

- Choose the workspace, exact source collection set and already loaded model
  explicitly. Changing the source scope clears the previous list and selection.
- Disable scope/model/record switching while an operation is pending. Errors use
  an alert. Recoverable review failures preserve the entered correction;
  unavailable sources clear the displayed artifact and correction. Other operation
  failures clear the displayed artifact. No operation silently retries or routes
  to another model.
- Review and outcome are separate one-shot actions. Corrections retain the original
  draft; outcomes have no preselected result. Reviewed does not mean resolved.
- Numbered citation links open the matching source disclosure; full IDs, source
  revision and model identity remain inspectable. Knowledge proposals are available
  only after accepted/corrected review and an outcome, and remain unapproved.
- Textareas use fixed adequate rows and internal scrolling. Preserve entered text
  during pending work; all controls have visible labels and normal keyboard access.

The Investigations view uses native select controls for the loaded local model,
answer profile, review decision, and outcome. The existing desktop Memory view uses the same
platform-owned select pattern. Native popup geometry and keyboard behavior are
accepted for this workflow; this decision does not establish a global select
owner for other views. Verify keyboard operation and the popup in the desktop
browser review.

New investigation briefs show exact source excerpts beside claims and link to the
matching raw or reviewed-claim source. Matching text is not a semantic correctness
guarantee. Model questions and hypotheses are labeled unverified; retrieval notices
are separate and cannot be edited. Historical combined missing-evidence lists keep
their unknown provenance label. Corrections preserve the original draft and must
include valid excerpts for facts and prior fixes. The investigation content panel
owns scrolling; citation navigation must not scroll the outer desktop shell.

The opt-in question-answer preview accepts up to eight optional questions; when
left blank, questions are planned from the issue. Answers and exact excerpts are
drafts for review: a supported status is a model claim, not proof of correctness.
Corrections keep the original questions and choose source snippets from the saved
catalog; the sidecar derives excerpts from those selections. Recoverable review
errors preserve edits, while an unavailable source clears the displayed artifact.
The preview defaults to the standard answer profile. Qwen3 non-thinking is an
explicit selection requiring a compatible loaded Qwen3 model; incompatibility
fails the request without falling back to another profile.

Reviewed calculations are an opt-in experimental path. The owner may author or
edit a plan of 1–12 distinct requests, or ask the loaded model to suggest one;
only **Confirm plan and draft** sends it for generation. Confirmation approves
neither the model's answers nor its source selections. Stale suggestions cannot
replace edits, and planning or generation failures preserve the plan. Extraction
notices and cited inputs remain inspectable. Calculation traces belong to the
original draft and do not change when an answer is corrected.

For v9 drafts, a correction may review each calculation using saved source
quantities in their operation roles, plus catalog excerpts and an explanation of
conflicts and authority. The preview is deterministic and does not establish
source truth. Changes to selections or explanation invalidate the preview; save
requires a fresh preview and retains its exact answer. Failed previews preserve
edits. Reviewer assessments and their traces remain separate from the immutable
original machine traces. Native selectors provide keyboard access to quantities.


## Reviewed knowledge pilot

`components/investigations/PilotKnowledge.tsx` owns folder consent, scanning,
source-bound proposals and explicit knowledge approval/supersession.
`views/InvestigationsView.tsx` owns pilot opt-in, selected scope, questions and
existing answer review/export. `components/investigations/controls.ts` owns the
shared Hearth field/button styles. The sidecar `company_brain_pilot` router is
responsible for privacy, scope, freshness and fixed answer configuration.

- Pilot mode is off by default. Registration, scanning, proposal creation and
  approval are separate actions. Supersession requires an explicit matching-key
  proposal, reviewer and reason; no background operation approves knowledge.
- Knowledge status and replacement selectors use the existing native Select/Listbox
  decision for Investigations. Labels, keyboard access, source disclosures, inline
  alerts and busy/empty/truncated states stay in the local investigation panel.
- A pending operation blocks duplicate activation and scope/model switches.
  Remounting on workspace/collection change prevents a late result from restoring
  old evidence. Errors preserve entered statement text while clearing potentially
  stale source and authority selections. Scans and knowledge changes clear the
  displayed answers; the sidecar revalidates saved records on access.
- Folder permission is explicit and resets when the path changes. Scanning explains
  invalidation. Typed reviewer names remain local-owner labels, not authenticated
  company roles. Pilot questions use one fixed measured path with no fallback.

Verification: `PilotKnowledge.test.tsx`, `InvestigationsView.test.tsx`, sidecar
`test_company_brain_pilot.py`, and the isolated synthetic browser walkthrough.
The browser preview certifies interaction only, not native inference or owner
acceptance. See [pilot instructions](docs/company-brain-pilot.md).


## Question-focused reviewed answers

`ReviewedPassageAnswer.tsx` owns the V3/V4 draft and its evidence disclosures.
Investigations owns the explicit, default-off **Focused passages (experimental)**
selection inside the reviewed knowledge pilot. Switching it clears previous
answers. It is unavailable during pending work. The older answer path is unchanged.

The short model-written draft appears first. Selected exact passages, complete
reviewed statements and exact source excerpts remain separately inspectable in
closed native disclosures. Source links use existing citation navigation. Markdown
uses the canonical safe external link; raw HTML and remote images do not execute.
Tables scroll within the content panel; orphan rows render as readable records.

A supported status is unverified. Unknown and conflicting leads describe only
the selected statements. Human corrections preserve the immutable original
presentation and appear without pretending that original selected prose was
recomputed. Shortening must never be graded as correct using hidden source text
to rescue missing prerequisites, exceptions or requested facts.

### Source context preparation

The reviewed-knowledge form uses the existing `PilotKnowledge` action/error model,
shared investigation fields/buttons and `ClaimContextPreview` for the literal
indexed-source preview. A keyboard-focusable bounded text region owns only its
own long-text scrolling; the surrounding form keeps its existing scroll owner.
Prepare is explicit; it never runs inference or writes a proposal. Adding context
preserves the existing draft and all context references, then closes the preview.
Draft/search/selection/scan edits invalidate it. Single-flight preparation and a
consumed-preview guard prevent duplicate actions. Errors preserve draft text,
clear stale selections and require fresh evidence lookup. Search clearing is
explicit, immediate and returns focus to the search input. Context citations stay
inspectable after insertion; server scope/revision validation remains authoritative.
Saving and approval/supersession keep their existing distinct lifecycle. No tokens,
locale support, navigation destination or network-routing policy changes.

### Assisted setup drafts

`PilotKnowledge` reuses the shared investigation action/error model and
`ClaimContextPreview` for optional question-derived source drafts. The question
appears before knowledge preparation. Suggestions use scoped indexed search,
prepare at most three complete indexed documents that fit existing limits, and
show missing/oversized/truncated candidates or omitted query terms. No partial
source fallback, inference, proposal or approval occurs in the suggestion action.
Applying a source prepends its literal text and unions its citations with the
current draft, checks the combined limits, and prevents duplicate application.

Suggested parts are literal question sentences, not verified semantic planning.
Applying them explicitly replaces the existing editor, enables reviewed parts
and clears confirmation. Manual editing and explicit confirmation remain required
before generation. Question/scope/scan changes hide stale suggestions; late
responses cannot restore them. Setup errors preserve entered statement text and
clear stale evidence selections. Existing native selectors, Hearth tokens,
keyboard/source disclosures and local-only routing remain authoritative.
