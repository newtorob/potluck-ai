# Company Brain local pilot

The desktop Investigations view now offers **Reviewed knowledge pilot**. It connects folder scanning, explicit knowledge review, a question, answer review and a saved Markdown handoff. It is off by default. This is a local-owner workflow; a workspace label or typed reviewer name does not authenticate a company role.

## Prepare documents and knowledge

1. Enable the pilot, enter a workspace, and choose **Load collections**.
2. Expand **Add a document folder**, enter a collection name and an existing absolute folder path on this machine. Confirm permission to read it, then register it. Registration does not scan or approve anything.
3. Choose **Scan** for that collection, then select every collection needed for the question. The pilot reads Markdown files and uses the existing source-collection exclusions for `.git`, `.obsidian` and archive folders. Symlink roots are rejected.
4. Search the selected documents. Inspect exact excerpts and their source identity. A limited result set does not establish that a missing fact is absent.
5. Select supporting excerpts, write a knowledge key and a statement retaining conditions and exceptions, then **Save proposal**. The statement remains proposed.
6. Inspect **Supporting sources**, choose **Review proposal**, and record the reviewer and reason before **Approve statement**. The typed name is an audit label for the local owner.

To replace approved knowledge, save a new proposal under the same key. Load Approved knowledge, choose **Replace statement**, inspect the proposed replacement and sources, and explicitly supersede the old statement. The old version remains history. A second approval cannot silently replace it.

## Ask, inspect and hand off

To use the reviewed fixed-parts path from the desktop, enable **Review question
parts before answering** under Question. Edit the initial part, add up to six
parts, then check **I reviewed these question parts**. Parts must be nonblank,
distinct and at most 500 characters each. The server assigns the IDs and checks
the exact question binding; confirming parts does not verify their coverage.

Changing the question, parts, model, workspace or collection context clears that
confirmation. An unavailable source or failed request keeps the entered parts
and requires confirmation again. The controls lock while a request is in flight;
duplicate clicks do not dispatch another request. This opt-in selects the V32
native profile through `/company-brain/pilot/ask-reviewed`, with no planning call,
automatic model load, fallback or retry. The earlier pilot options remain
available when this control is off.

Choose the already loaded compatible Qwen3.8 27B native model, enter one question (up to 500 characters), then **Ask reviewed knowledge**. This explicit pilot fixes the measured V73 answer configuration; it does not load a model, change the ordinary gateway, or fall back to another route. If the runtime is unavailable or incompatible, the request fails with no automatic retry.

For a question-focused draft, explicitly enable **Focused passages (experimental)** before asking. This selects the separately versioned focused presentation (currently V4, V75 correction). It writes a short answer, saves exact selected passages with their governing structural context, and preserves full reviewed statements. The checkbox defaults off; changing it clears the displayed answer. It does not change retrieval, approve any statement or verify the generated answer.

Inspect the draft first, then expand **Selected source passages**, **Full reviewed statements** or **Exact source excerpts** as needed. Unknown and conflicting answers carry explicit selected-statement scope. Exact citations do not establish that the draft retained every condition or answered the question correctly. Accept, correct or reject the text itself.

Inspect the answer, cited excerpts, retrieval limits and original draft. Use **Accept**, **Correct** or **Reject** with a reviewer and reason. Corrections retain the original draft. **Record actual outcome** is separate and has no default result. Export Markdown to save the answer and its review evidence for handoff. A review decision is not evidence that the underlying business issue was resolved.

## Fixed-part review API

`POST /company-brain/pilot/ask-reviewed` explicitly selects
`fixed-part-selection-v32`. The existing desktop controls and `/ask` route keep
their previous defaults. This separate route accepts the same exact workspace,
collection IDs, loaded model ID and question, plus:

```json
{
  "question_parts": ["What is required?", "What exact deadline is stated?"],
  "question_parts_confirmed": true,
  "pilot_confirmed": true
}
```

Supply one to six distinct, nonblank parts, each at most 500 characters. The
caller must inspect and explicitly confirm those parts. The application assigns
part IDs and stores the exact question-bound plan with `reviewer_supplied`
origin and `coverage: not_verified`; confirmation does not prove completeness
or approve knowledge. Authentication, privacy and current-source checks apply.
The desktop question-parts editor uses this same explicit API integration.

The route fixes the native profile, seed, approved-collection retrieval and V32
contract. The service makes two bounded 256-token model calls, preserves both
actual replies and replays their source/plan bindings on save and read. It does
not load a model or retry. Approved source-unit plans are used when present;
otherwise a reviewed statement remains one whole source unit. No source-unit
plan is silently authored or approved. That fallback differs from the curated
plans in the qualification and needs its own delivered-answer review.

Answer review, correction, outcome and export use the existing investigation
endpoints. Reviewer names remain local audit labels, not verified company roles.
Use a separate data directory and a non-8321 loopback port for an isolated pilot.

## Readable fixed-part answers

V32 answers have a deterministic readable view in the desktop and Markdown export.
Identical leading paragraphs from the same source revision are shown once, with
the evidence groups they apply to. Standalone table rows become labelled fields
only when the exact row has unambiguous headers in its cited source. Missing or
ambiguous context keeps the original text. Conditions, question/status leads and
citation bindings remain intact; this view makes no model call.

**Original generated answer**, selected passages and exact excerpts retain the
unaltered material. `readable_answers` is a derived response field, never stored
authority. The desktop checks its original text, status and snippet IDs before
using it. A correction takes precedence over an earlier readable draft.
`GET /company-brain/investigations/{id}/export?layout=original` retains the legacy
handoff. The default V32 export presents the readable view with literal original
answer and excerpts in an audit disclosure; corrected briefs use the existing
correction export. Other generation modes and ordinary gateway defaults stay as
configured.

## Freshness and recovery

- Changing workspace or selected collections clears the previous artifact and pilot draft context. Finish or copy an unsaved statement before changing scope.
- During requests, controls prevent duplicate actions and scope/model switching. Recoverable errors retain entered statement text but clear source selections and loaded authority decisions so evidence can be checked again.
- Rescanning changed documents invalidates their reviewed knowledge and dependent answers. Re-read and review current evidence. Old citations cannot be reused to approve stale content.
- The sidecar checks privacy settings, authentication, exact collection scope and source freshness on each operation. The UI does not replace those checks.
- Evidence search is capped at 20 results. Knowledge listing examines at most 101 in-scope rows, returns at most 100 valid claims and reports truncation; it is not an exhaustive knowledge browser.

## Qualification boundary

The prior V73 result was 19/19 questions over six already inspected historical documents (17 consumed regressions and two fresh questions on those same documents). It was not an unseen-company evaluation. This pilot adds a usable path to that configuration; component tests and a synthetic browser preview are not new model-quality scores.

The bounded native pilot is complete and Rob accepted its actual output with a
readability follow-up. It exercised one known historical-document question
before/after an identical-content revision, explicit supersession, primary review,
export and stale-source rejection in a disposable copy. Broader unseen-document
evaluation, company access control and production readiness remain separate.

The V32 correction build separately passed 69/69 factual and 69/69 presentation
reviews plus 2/2 diagnostics on known historical OfferLume documents, with fixed
question/source plans and synthetic knowledge approvals. Its sealed evidence is
summarized in [the public milestone record](../bench/results/company-brain-milestones-20261111/README.md).
Rob accepted that bounded build on November 11. The V32 pilot API integration
adds no native answer-quality score and does not turn that build acceptance into
approval of individual knowledge statements or current company policy.

Detailed source corpora, raw model replies and local databases are retained with
the owner. The public summary states conditions and limits without redistributing
those company documents. Public synthetic fixtures and regression tests live in
`apps/desktop/python/evals/company_brain` and `apps/desktop/python/tests`.

When preparing reviewed excerpts, retain the source title and its stated date or
status alongside each selected section. A document's verification date, draft
label or historical scope can change how its claims apply. A citation to the
full document does not restore a qualification omitted from the text supplied
to the model. Keep prerequisite and exception paragraphs with their governing
rule. If the document gives no date, say so during review rather than inventing
one. Knowledge review still decides whether the excerpt is suitable to admit.

### Prepare source context

After selecting supporting excerpts, choose **Prepare source context**. The local
pilot reads the document's leading indexed section and the selected excerpts
from the same workspace, collections and source revisions. Review the literal
text, then choose **Add context and excerpts to draft**. It prefixes the prepared
text to your existing draft and adds every supporting context citation. The
**Draft source citations** disclosure includes the added opening text. Saving a
proposal and approving knowledge remain separate explicit actions.

Draft, search, selection and scanned-source changes invalidate a preview. Errors
retain the draft but clear source selections, requiring a fresh lookup. No
inference, background approval, automatic retry or modification of the original
files is involved.

This is an indexed opening, not date/status classification or reconstruction of
the original Markdown layout. The existing parser determines stored headings.
Leading untitled chunks and the first named indexed section are included;
conditions or dates elsewhere still require full-source review. Unscanned disk
edits are outside the indexed snapshot and require a rescan.

`POST /company-brain/pilot/claim-context` takes the normal explicit scope and
1–16 distinct source citation IDs. It requires all selected collections to be
ready/recent and every reference to match the current document revision. The
preview includes at most four opening chunks and 6,000 heading/text characters
per document, sixteen expanded citations and 16,000 prepared characters. An
incomplete or oversized opening/excerpt fails the preview rather than returning
silently shortened context. The desktop also blocks insertion if combining it
with an existing draft exceeds the proposal limits. Choose fewer/shorter excerpts
or review the full source manually. Source text remains unapproved data until a
separate review decision; the original manual proposal route remains available.
