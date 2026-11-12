# Source context intake: bounded review build

The desktop reviewed-knowledge pilot can now prepare an indexed document opening
alongside selected excerpts, before saving a statement. This addresses an observed
curation failure where a selected passage omitted a source date. Reviewers see
the literal text and explicitly add it, with all context references, ahead of any
existing draft. No model inference or automatic knowledge approval is involved.

The preview is deliberately limited to the existing index. It does not identify
which date is effective, classify policy status or recover Markdown structure the
parser did not retain. Reviewers still inspect the full document for conditions
outside its opening. Oversized or incomplete previews fail rather than silently
cutting source text. Existing manual proposals and approval/supersession remain
available. See [pilot controls and limits](company-brain-pilot.md#prepare-source-context).

## Validation

| Check | Result |
| --- | --- |
| Full sidecar suite | 9,339 passed, 5 existing skips, 1 existing dependency warning |
| Additional fifth-opening-chunk guard | 1 passed; a draft label beyond the opening limit cannot be silently omitted |
| Frontend | 550 passed; final typecheck and production build passed |
| CLI | 1,355 passed after replacing a timing-sensitive concurrency assertion setup with a controlled overlap barrier |
| Landing | 44 passed and typecheck passed; no landing production build in this batch |
| Privacy lint | Passed |
| Browser workflow | 2 passed: explicit preparation, loading, edited-preview invalidation, keyboard insertion, exact proposal payload, stale-source recovery and search clearing |
| Browser presentation | 1,152 px dark, 800 px light, reduced motion and forced colors inspected; narrow action remains keyboard reachable |
| Consumed document intake replay | 2 passed through real authenticated pilot routes; missing excerpt dates/status restored in proposed statements with bound context citations; zero approvals/inference |
| Scoped UI audit | Strict pass after adding standard scrollbar fallback with existing Hearth tokens; Chromium screenshots unchanged |

The complete sidecar suite ran before adding the extra fifth-chunk test; that
new test was then run separately. The initial frontend suite passed but its
following typecheck caught an unchecked test-fixture array access, corrected
without changing product behavior. Initial CLI execution exposed an existing
15 ms overlap assumption; the corrected test holds both slots until overlap is
observed and retains the original concurrency/outcome assertions. These failures
and their corrections are retained locally. A broad unconfigured monorepo static
scan was stopped and rerun with explicit feature/CSS source roots.

One bounded owned-local Potluck CLI agent reviewed the generic intake contract.
Its packet was rejected for invalid field names, and primary review also found
that one response addressed the wrong criterion. It contributed zero accepted
findings or test cases. The raw failure remains unchanged, with no output repair,
contract relaxation or retry. The single call measured 630 input and 188 output
tokens; the runtime lifecycle took 75.75 seconds. Independent cleanup verified
all four tracked processes gone and the isolated port closed. Earlier useful
agent cases remain separate evidence, not general agent qualification.

This batch changes intake/review, not inference, source parsing, the database
schema, ordinary gateway routing or model defaults. Older answer scores remain
unchanged. Browser APIs were mocked; the two real-route intake replays use
consumed same-company documents and do not qualify new model answers. Private
sources, databases, raw replies and complete archives remain local. Testing was
on macOS Chromium and the existing owned Linux agent runtime, not native
installer or cross-platform certification. No cloud CI or release occurred.

## Prepared-knowledge native workflow follow-up

A subsequent bounded workflow carries prepared statements through actual native
answers, artifact-bound primary AI review, inconclusive outcomes, exports,
isolated source invalidation and replacement knowledge preparation. Three known
questions pass their frozen factual/scope/presentation criteria; one same-question
refresh also passes with unchanged historical wording and new source bindings.
The unrelated claim stays approved, while all three old answers/exports become
unavailable because both collections were in their scope. This tests a labelled
source byte change, not a change in policy meaning.

The run used consumed documents, five curated source selectors and supplied
question parts. Both statements reached every native prompt. Knowledge approvals
and answer reviews were primary AI test fixtures, not new owner acceptance or
current-policy approval. Human effort was not measured. All prior scores and
failures remain unchanged. Detailed conditions, measurements and private evidence
binding are in the [public workflow summary](../bench/results/company-brain-prepared-knowledge-20261112/README.md).

## Optional setup suggestions

In the reviewed-knowledge pilot, enter the question above the knowledge form and
choose **Suggest sources and question parts**. This read-only action uses the
existing scoped word-overlap search; it does not run a model. Up to three matching
documents are offered. When a document fits the existing sixteen-citation and
16,000-character preview limits, its complete indexed chunks are prepared with
the indexed opening. Oversized documents stay on the manual search/review path;
no partial document is silently substituted. Search limits, omitted terms and
missing results are shown. Suggestions do not prove relevance or corpus coverage.

Inspect a candidate, then explicitly add its text and citations before the
existing statement draft. Enter a knowledge key and use the existing proposal
and approval controls separately. The part suggestion splits only literal question
sentences. Quoted punctuation, repeated parts, excess sentences and compound
requests may retain the full question; edit shared conditions and coverage before
confirming. **Use suggested question parts** replaces the editor contents and
clears its confirmation. It approves neither knowledge nor an answer.

Question, collection and scan changes invalidate displayed suggestions. Late
responses cannot restore a different question's suggestions, and the existing
server proposal route revalidates source revisions. Existing manual search and
question editing remain available. The ordinary answer path, model, prompts,
source parser and approval authority are unchanged. Native results for the new
setup path are recorded separately after the workflow is frozen.

### Measured setup batch

The frozen setup path found the complete required source within three suggestions
for three new-to-recorded-evaluation documents and used six generated literal
parts unchanged. All three original answers pass the frozen factual/scope criteria
under primary AI review. Human effort remains unmeasured and complete-document
answers remain long. The read-only local-agent packet failed its scalar-input
contract and was rejected without repair or adoption. See the
[public setup evidence summary](../bench/results/company-brain-assisted-setup-20261112/README.md)
for run conditions, checks, retained failures and qualification limits. Older
scores and default inference behavior remain unchanged.
