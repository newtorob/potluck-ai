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

`POST /company-brain/pilot/ask-reviewed` defaults to
`fixed-part-selection-v32`. Callers can explicitly select the qualified V33
observation-scope review with `reviewed_answer_version: "v33"`. The guided
Company Brain desktop entry selects V33; Advanced and `/ask` retain their
previous defaults. This separate route accepts the same exact workspace,
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

The route fixes the native profile, seed and approved-collection retrieval.
V33 keeps the V32 wire contract and adds review instructions to distinguish
known or observed events from an actual total when observation is incomplete.
A complete explicit count or scoped negative can still support an answer.
Missing requested facts remain unknown within the selected reviewed statements;
this instruction is not an independent correctness check. Older saved answers
keep their original version and verdict. The service makes two bounded
256-token model calls, preserves both
actual replies and replays their source/plan bindings on save and read. It does
not load a model or retry. Approved source-unit plans are used when present;
otherwise a reviewed statement remains one whole source unit. No source-unit
plan is silently authored or approved. That fallback differs from the curated
plans in the qualification and needs its own delivered-answer review.

Answer review, correction, outcome and export use the existing investigation
endpoints. Reviewer names remain local audit labels, not verified company roles.
Use a separate data directory and a non-8321 loopback port for an isolated pilot.

### Unresolved clauses (explicit V34 opt-in)

`reviewed_answer_version: "v34"` adds a required `unresolved` list to each
proposal and review part. Fragments must be exact, uniquely occurring substrings
of that part's request, in question order without overlap. The server binds
their character ranges; it does not generate a replacement question or claim
the model found every missing fact. An unresolved clause prevents a Supported
verdict. Conflicting evidence retains its existing priority, with unresolved
clauses still visible. A final Unknown must identify an unresolved clause and
retain the existing selected-source scope basis.

The answer names what was not established by the selected reviewed statements
before presenting the known facts and their complete conditions and citations.
Its readable view keeps this notice and known evidence visible; full statements,
exact excerpts and the original draft remain expandable. Corrections, reopen
and export replay the V34 bindings. Existing saved V32/V33 answers and caller
defaults are unchanged. An explicit guided-view V34 option is forwarded to the
same API contract; omitting the option retains the existing default. This opt-in
is awaiting fresh native qualification;
valid structure and source binding do not establish answer correctness.

## Readable fixed-part answers

The explicit experimental `reviewed_answer_version: "v35"` keeps the V34 wire,
literal unresolved clauses and evidence guards. Its second call audits requested
fact coverage before trimming redundant source units. V34 prompts and saved
replays remain unchanged. V35 awaits its separate frozen qualification; the
desktop caller, raw API and Advanced defaults remain unchanged.

The separate experimental V36 contract keeps each confirmed question part intact.
Its first call proposes literal clause fragments. The server binds them in order,
restoring only original punctuation and whitespace separators; the bound clauses
must join to reproduce every original character. No omitted words are allowed.
Its second call checks every clause against the supplied statements;
the server derives the original part verdict and scoped unresolved clauses. The
partition remains a model assessment, not verified semantic coverage. Both replies,
partition equality, source closure and measured 256/512-token stage caps replay
before saving, reopening, correcting or exporting. V36 remains opt-in pending its
separate native qualification; earlier saved contracts and defaults are unchanged.

V32, V33 and explicitly selected V34 answers have a deterministic readable view in the desktop and Markdown export.
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

### Confirmation regression coverage

The [November 13 guard tests](../bench/results/company-brain-confirmation-coverage-20261113/README.md)
exercise quoted Unicode and compound requests through actual setup/reviewed-ask
routes and the desktop view. Suggested parts preserve one whole request; setup
does not approve knowledge or ask the model. An unconfirmed ask is rejected.
Applying or reapplying suggested parts, and editing confirmed parts, requires new
review before dispatch. These are executable regression tests with generation
doubled explicitly; they establish no new answer-quality or operator-effort score.

## Return to a workspace and review changes

Potluck remembers the last workspace, source selection and model choice locally
and in the sidecar settings. Reopening rechecks the selected collections. A saved
model that is no longer loaded stays marked unavailable; load it or choose the
current model explicitly. Questions, confirmations and source approvals are not
preferences and are never recreated by reopening.

Use **Scan documents** to check registered folders. Each collection keeps its last
successful scan report across restarts, with document paths and change counts.
An unchanged scan preserves approved knowledge. A changed or removed document
revokes its source-bound statements. The current conservative invalidation also
clears every saved answer whose scope includes that collection. Reindexed files
(for example after an explicit parser change) follow the same review lifecycle.
Use **Find source evidence** to inspect the new revision, propose its statements
and explicitly approve them before asking again. An unchanged document's current
approved statements can inform another question without another approval.

The report is an indexed snapshot, not a background file watcher. Freshness and
current source/review checks remain enforced by the sidecar on every operation.

Scan reports retain up to 100 redacted relative paths per collection, together
with counts for the complete scan. Removing the collection deletes its report.
The report contains no source document or answer text. It records the last
successful scan only; it is not an approval queue or a full revision history.

## Guided desktop return visits

Open **Company Brain** in the sidebar. Your last workspace and selected folders
reopen. On a first visit, choose **Open workspace**, choose a folder, explicitly
allow reading it, and add/scan it. The supported loaded local model is shown under
**Workspace and folders**; this page never downloads or loads a model.

Enter a question and choose **Review sources for this question**. Inspect complete
source drafts before clicking **Approve document for answers**. Exact current
approvals need no second approval. Suggestions do not narrow the answer scope:
answers use current approved knowledge in all selected folders. Continue with
that knowledge, review the question parts, confirm them and choose **Get answer**.

**Recent questions** reopen saved answers without inference. **Document changes**
shows the last successful scan and offers **Review [document]** for current added
or updated paths, without replacing your question. Removed documents cannot be
reapproved from that entry. Oversized or ambiguous replacements lead to **Advanced
investigations**, where the existing correction, supersession and outcome tools
remain available. A source or answer that is no longer current must be reviewed
again; it is never revived from the browser cache.

Opening **Advanced investigations** from an answer hands that record to the
existing review tools under its exact saved folder scope. Returning preserves
the question in this app session and requires fresh source/question confirmation;
these drafts and confirmations are not written to durable preferences.

**Full supplied evidence** includes the reviewed statements supplied to the answer,
including statements the response did not cite. The inventory shows the count and
source identities with each statement initially collapsed. A citation opens its
uniquely matching statement or source; an unavailable or ambiguous match returns
to the full inventory for manual review. Complete statements and raw source text
remain available without generating a summary. The original answer remains available.

## Source suggestions

The guided view requests the versioned `document-topics-v4` suggestion method.
It ranks normalized topic words in document names, headings and indexed text;
shared frontmatter and Related wiki-link footers do not add ranking weight.
Each suggested document shows its matching words. These are inspectable lexical
matches, not verified relevance, approval or confidence scores.

Review is bounded to 200 documents, sixteen chunks and 16,000 ranking characters
per document, with at most three suggestions. The UI reports incomplete suggestions;
missing matches cannot establish corpus-wide absence. Refine the question or use
Advanced investigations for other documents. Complete previews still use every
indexed chunk within the existing complete-source caps, including all metadata,
conditions, exceptions and citations. A rank never grants approval or restricts
answers to suggested documents: answers use all current approved statements in
the selected folders. The API's omitted-version default and Advanced source setup
retain the legacy suggestion method; answer retrieval and model settings are unchanged.

## Desktop setup and scan reports

The guided workspace keeps engine connection, selected folders and loaded model
status visible above the question. Connection checks use GET `/health` only.
Retry never loads a model, scans sources, approves knowledge or runs inference.
Startup restoration is shown as pending or failed rather than as a loaded model.
Source review works without a loaded model; answers require the current connected
model to match the explicit selection. A connection/model change clears question
confirmation. The server still enforces the supported native profile and freshness.

Folders need a successful scan within the existing 24-hour source snapshot window.
The frontend provides early guidance; server validation is final. Document-change
paths start collapsed in a bounded scroll area. Last-successful scan totals and
revoked-statement/cleared-answer counts remain visible. Failed scans label the prior
report, removed paths have no review action, and bounded-report notices stay visible.
Opening setup or expanding paths does not scan, approve, infer or change scope.

The supplied-evidence panel renders raw text literally, preserving string values verbatim. Other JSON
values remain inspectable as JSON rather than displaying object coercion labels;
this changes no source bindings, answer generation or authority.

## Bounded reviewed evidence

The guided desktop requests `reviewed_evidence_version: v6` after its narrow
qualification. The HTTP API still defaults to v5; an explicit v5 caller override
and Advanced investigations retain the older path. V6 reuses v5 ranking and
character limits and budgets actual deduplicated paragraph snippets at whole
reviewed-statement boundaries. Selected statements, their supporting text,
conditions and citations remain complete. Lower-ranked statements may be omitted;
counts are visible above the answer and exact selection/omission traces stay in
provenance. The 100-snippet ceiling is unchanged. An oversized highest-ranked
statement still fails closed. Bounded selection cannot prove whole-company absence.

The statement inventory, including omitted records and their source bindings, is
rechecked before the answer is saved. Opening, exporting and organizing answers
never reruns inference or approves policy. Generic before-event questions do not
automatically become before-building questions when organizing numbered source
steps; explicit implementation, after-boundary, completion and conditional rules
remain guarded. Source statements still require factual and relevance review.

Review-effort pilots record approvals/reuse, manual part edits, source text
presented, API wait and native generation time separately. Assistant wall spans
include tool and inspection delays; they are not active human minutes. These
measurements do not establish customer usefulness or time saved.
## Guided question setup

Type the complete question once and choose **Review sources for this question**.
After source review, **What the answer should cover** shows literal suggested
parts. Review the list and confirm it; editing or adding parts is optional.
**Edit question parts**, **Keep as one question** and **Restore suggestions**
let you adjust the list without retyping the original question. Each adjustment
clears confirmation. Sources still require explicit approval before answering.

Suggestions separate simple explicit question clauses without a model call or
paraphrasing. Ambiguous wording, quotes, lists, conditions and exceptions stay
together as one question. The original wording and separators remain intact;
coverage is still unverified. The existing six-part/500-character bounds apply.
Question-part edits survive recoverable source preparation, scan and same-scope
workspace refresh failures. A changed question or folder scope requires fresh
setup. This frontend workflow does not change backend selection, model routing,
saved answer text, citations, source freshness or authority checks.

Guided reading keeps all supported cited context visible by default, including conditions. A narrowly bound single-part unknown can show its scoped verdict and citation first; open **Scope evidence and original answer** for the complete original. Other unknowns retain the full existing display. This changes display only, not the saved answer or source authority.


### Source sections in guided answers

A supported guided answer may show the approved document's own section headings
above its cited passages. This requires explicit approved/recent evidence metadata, one exact document revision, unambiguous
heading and source-chunk bindings, and passages in their original source order.
The interface does not invent answer categories or rewrite the passages.
Missing or ambiguous metadata keeps the existing reading view. Additional cited
context stays open; full evidence and the original answer remain expandable.
These headings organize source text and do not establish factual correctness or
current policy. Saved answers, exports, model requests and source approvals are
unchanged.


### Review and hand off a guided answer

Open a saved question from **Recent questions**. No model needs to be loaded to
review or export an existing answer. Choose **Review and export this answer** to
jump to the controls, select Accept, Correct or Reject, and enter a reviewer
label and reason. No decision or reviewer is selected for you. Saving a review
does not approve source knowledge or establish a real-world outcome.

For the ordinary V32/V33 and opt-in V34 single-answer path, Correct lets you edit the text and
verdict while retaining the existing question and source references. Complex
answers, calculations and changes to selected sources remain in **Advanced
investigation**. The server validates the correction and current source scope.
The corrected text becomes the current reading, with linked sources bound to
its saved excerpts. **Original draft before correction** retains the original
answer and citations. Missing or mismatched excerpt bindings do not create
source links.

**Export Markdown** returns the canonical handoff, including the saved review
and evidence. Its read-only text can be copied or selected manually. Exporting
a draft does not accept it. Review/export errors keep entered work. After an
uncertain write, the app checks the saved record once rather than automatically
submitting again. If sources are unavailable, review/export is disabled until
the source folders are checked and the answer reopened. Reviewer names are
local labels, not authenticated company roles. No review time, usefulness or
actual outcome is inferred.


### Private pilot resource verification

Opt-in pilot builds validate the pinned standalone engine before Analysis, then
copy those exact executables after Analysis so PyInstaller does not rewrite their
Mach-O identity. A post-collection check requires every original engine/resource
hash before running architecture, system-dependency and version/help probes
again. The marker retains both input and frozen hashes; a different executable
cannot pass by retaining the same manifest. Ordinary builds keep their existing
engine handling.

The after-pack check compares the complete external Python/MCP inventory and
file hashes. Relative framework directory aliases are allowed only when both
staged and packaged links match and resolve inside their respective bundles.
Their underlying files remain independently hash checked. Absolute or escaping
links, changed target bytes, missing resources and lost executable permissions
are refused. These are packaging checks, not proof of answer quality, signing,
notarization or external-company acceptance.


## Experimental typed findings (V37)

The explicit `reviewed_answer_version: "v37"` candidate partitions the confirmed
question literally, proposes typed findings, then independently audits them.
The three bounded calls reserve 256, 512 and 512 output tokens. Raw replies,
source scope, clauses and stage receipts replay independently from application
conversion. Literal coverage and valid source IDs do not prove answer correctness.

Known rules retain exact source conditions and dependency closure. Missing world
rules, permissions, actual totals or values remain Unknown with their literal
clause and visible selected-source scope. A source-text-content finding can
acknowledge whether the underlying requested value is established by the selected
reviewed statements; this does not claim exhaustive company-wide absence or turn
silence into prohibition. Those classifications remain unverified model judgments.

Set `organize_answer: false`: V37 has no model-written outline or extra fourth call.
The frontend helper supplies that value for explicit V37 requests; older caller
defaults stay unchanged. A raw request for organization fails before inference
with a recovery message. Saved originals, exact citations and full expandable
evidence remain intact when a separate correction is reviewed. V37 is opt-in and
unqualified until frozen consumed regressions and untouched cases pass, followed
by matching private desktop and clean-environment checks. No default promotion.
