# Model recommendations

The desktop first-run picker and CLI setup consume `GET /models/setup-options`.
`?use=general|coding|writing|reasoning` selects intended use for that request;
the response echoes `useCase`. Nothing is downloaded, loaded or selected in the
runtime by this read. Explicit CLI model choices and project pins remain authoritative.

`recommended` contains up to three unique IDs in engine order. Both clients
exclude failed answer checks and models whose fit is not explicitly true. An empty
ranked list never falls back to the first catalog entry. `recommendedModel` retains
the legacy ordering for older clients; new clients use the ranked list.

The engine checks available memory and download space, using measurements from
this machine when available. Passing answer checks outrank untested models. Within
those evidence classes, catalog `use_cases` tags guide task matching. Untested
choices favor the largest catalog tier inside the estimated GPU budget, then
task match, loaded, featured and installed choices. CPU execution starts smaller.
Apple unified memory is one pool. Linux GPU capacity must be verified by the
runtime. A failed check or insufficient resources cannot be overridden by a tag.

Tags are ordered editorial guidance, not task benchmark results. They do not
establish coding quality, tool reliability, speed or usable context. Cached feeds
without tags inherit bundled tags only for the same slug and SHA-256 artifact.
Explicit feed tags, including an empty list, take precedence.

## Desktop behavior and ownership

`services/model_setup.py` owns eligibility and ranking. `localApi.ts` owns timed
HTTP requests. `useModelRecommendations` cancels superseded requests and hides old
results while a new purpose or retry is pending. `modelRecommendations.ts` checks
the response contract before the picker can offer an action. An older engine gets
an update message rather than a misleading tier fallback.

The existing Hearth tokens in `styles/globals.css` and `tailwind.config.ts` remain
the visual source of truth. `ModelPick` uses native radio groups for purpose and
model choice, retaining platform keyboard behavior and visible focus. The wizard
body scrolls within the viewport while its actions remain reachable. Existing
`DownloadProgress` and `modelStore` own transfer progress and runtime actions.

Only **Download and load** requests a transfer followed by a load. Previously
completed transfers never auto-load just because their status is present. All
choices are locked during a transfer. Load failures remain on the error step and
require an explicit retry. The shared store records errors rather than rejecting
its promise, so the wizard checks that result before advancing. Enter on the page
cannot bypass model selection or loading. Skip marks onboarding complete without
requesting a download or load.

## Verification

Engine tests cover actual bundled catalog ordering, memory/disk constraints,
unknown hardware, GPU verification, matching artifact metadata and proof priority.
CLI tests cover flags, intended-use setup, explicit choices, empty results and
older runtimes. Frontend tests cover response order, estimates versus evidence,
stale requests, explicit consent, installation/load errors and duplicate prevention.
`e2e/onboarding.spec.ts` exercises keyboard controls, both themes, a narrow viewport,
empty results, failure recovery and skip against mocked backends. Its screenshots
show illustrative catalog data, not a machine benchmark.
