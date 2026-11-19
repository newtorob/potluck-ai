For the current explicit V32 pilot and readable display, see [the pilot guide](company-brain-pilot.md). This document describes the original fixed-parts contract and preserved lineage.

# Explicit question parts

The internal `investigation_request_parts` and `investigation_part_selection`
modules separate declared question parts from source selection.
`investigation_fixed_part_answers` connects them to opt-in investigation service
mode `fixed-part-selection-v1`. The HTTP create schema, UI, existing modes and
defaults are unchanged. This is an internal measured candidate, not a public
or qualified Company Brain capability.

The application assigns P1, P2 and subsequent IDs to a supplied ordered list of
part requests. It binds that list to the exact sanitized question, factual
request and origin. Each selection and review response must contain every fixed
key. Models select source-unit IDs and verdicts; they cannot invent boundaries,
omit an expected part or change its text. JSON property order does not control
part order. A changed plan has a different digest; a digest is not a signature
or an approval. Question parts containing credentials are rejected before model
measurement or dispatch rather than silently changing the declared plan.

The input can originate from a fixture, reviewer or model proposal. Every origin
retains `coverage: not_verified`. A structurally valid incomplete plan remains
incomplete. Automatic decomposition and question coverage must be evaluated
separately from answering with a supplied plan. Neither a fixture nor a model's
`supported` verdict establishes truth or current policy authority.

The selection path reuses the reviewed claim validator and source-unit plan
primitive. It closes declared dependencies, preserves exact source text and
ranges, and rejects missing/extra/duplicate response keys, unknown/duplicate unit
IDs, insufficient evidence cardinality and capacity overflow. Each part retains
its own supported/unknown/conflicting assessment. Unknown parts can retain
relevant known context; negative or deferred statements can be supported.
Missing-detail wording is limited to selected reviewed statements. A nearby
quantity can still be selected incorrectly: semantic review remains necessary.

## Service boundary and budgets

Call `create_investigation` internally with `question_part_plan`, presentation
mode `fixed-part-selection-v1`, template `investigation-v21`, one supplied
reviewed `answer` operation, reviewed evidence selection, compact encoding,
`bounded-evidence-v1` and `qwen3.8-27b-native-non-thinking`. Other operation types,
missing plans, mismatched sanitized question/request, and plans supplied to other
modes fail before inference. Supplying a plan does not establish that a human
reviewed it; its declared origin and unverified coverage remain visible.

The first call proposes units with a 2000-token output cap. The independent
review call assigns verdicts and units to every fixed part with a 512-token
output cap. Each stage measures its actual messages and response schema through
the pinned runtime, reserves output in the same context capacity, and shares the
existing request deadline. The full prepared-input digest binds complete support
and plans; the prompt/schema digests bind what was actually sent. Supporting
source excerpts remain in saved input and are explicitly absent from these
model messages. No input clipping, schema repair, hidden retry or extra planning
call is added. Overflow, identity changes, malformed stages and output truncation
fail without saving an investigation.

Cancellation during generation propagates, leaves no saved draft, and does not
resume or retry the unfinished model turn. A later explicit create starts a new
request. This does not add a durable worker queue, idempotent command admission,
background recovery or a guarantee that cancellation after a committed save can
undo that save. Native token-level cancellation behavior remains the existing
runtime's responsibility.

## Saved originals, reviews and current sources

The save transaction stores a mode marker plus an independent question-plan
payload binding in nullable opt-in columns. Existing rows remain unchanged.
Reopen, review, outcome and export reconstruct the original answer and exact
source/snippet mapping from the saved raw stages and original inputs. Prompt,
schema, whole-input, part-plan and measured budget receipts must agree. The
independent binding rejects self-consistent replacement plans in provenance.
Retained answer markers also prevent silently downgrading a fixed-part record by
removing both storage markers and provenance labels. These checks detect
inconsistency; they do not authenticate arbitrary rewrites of the entire local
owner-controlled database.

Source content, approval and approved-unit-plan receipts are reread inside the
authoritative transaction, after inference has released the database lock.
The absence of an approved unit plan is frozen too: a later approval or
supersession invalidates the saved source view. Sources changing after an
initial read cannot authorize a review or outcome write. Outcome writes also
compare the reviewed artifact for this mode.

The generated draft stays unchanged when a human corrects it. The correction is
stored separately, loses generated presentation metadata, and is revalidated
against the original questions and allowed source snippets when reopened or
exported. A valid correction remains human-authored, not automatically verified
for truth. Original `needs_review` and unverified-coverage flags describe model
output; only the separate review record records a human action. Automatic draft
verification remains explicitly unsupported for this selection contract.

## Qualification

Offline checks use synthetic fake-runtime responses on macOS, without native
model calls. They establish contract and lifecycle behavior, not answer quality,
Linux/Windows support, current company-policy authority or owner acceptance.
Freeze implementation, consumed inputs, rubrics and runtime limits before
separately frozen fresh controls and any bounded serial native assessment.
Report supplied-plan accuracy and automatic-plan coverage separately. Preserve
all earlier questions, failures and scores. Exact source rendering remains the
canonical answer; concise presentation is a separate later view.


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


## Experimental direct source questions (V38)

V37 remains unqualified: its first frozen phase passed, but a held-out case
produced a misleading source-content acknowledgement despite retaining the source.
The original failure and all earlier scores remain unchanged.

Explicit `reviewed_answer_version: "v38"` separates direct yes/no answers about
the supplied text from establishment findings for substantive rules, values,
permissions, causes and events. This includes negative question wording. A
source-content No is scoped to the selected reviewed statements; missing actual
permission stays Unknown. Known conditions and citations remain exact source
text, not a generated summary. An incompatible finding keeps priority.

The three stages reserve256/512/512outputtokens. A supported string sentinel
marks the unused decision field; the runtime schema guard is unchanged. All raw
replies, typed decisions, derived conversions, source freshness and measured
receipts replay separately. Corrections stay separate from immutable originals.
Use `organize_answer: false`; no fourth model outline is supported. The frontend
helper opts out for explicit V38 requests, leaving prior caller defaults intact.
V38 is opt-in and unqualified until its committed source, unchanged regression
rubrics and untouched cases pass a separately frozen assessment. Matching
desktop and clean-environment checks follow; no production promotion is implied.


## Experimental bound-question audit (V39)

V38 remains unqualified: its unverified proposal contained a redundant unused
field, which strict validation rejected before the independent audit. Its native
failure remains separate from passed local checks.

Explicit `reviewed_answer_version: "v39"` admits structurally closed, source-bound
unverified proposals, then binds their inferred question types for the selected
decision audit. The audit can correct decisions and citations within that question
or flag a mapping it cannot use; it cannot silently answer another proposition.
The inferred types remain unverified and do not grant source or fact authority.
The original whole question and literal parts stay unchanged.

A mapping-unclear result refuses before save with a recovery message. It is not
a claim that the source lacks a fact or that source evidence conflicts. Missing
actual facts still produce scoped Unknown answers; incompatible same-property
source alternatives retain priority. Fixed source-content Yes/No answers retain
literal polarity, exact conditions, numbered citations and selected-statement scope.
Raw proposals, derived type bindings, ignored fields and final native decisions
replay separately. Corrections remain separate from immutable originals.

The three calls retain256/512/512output budgets. The first two model input
contracts remain V36 and V38; the selected-decision audit uses V39. Set
`organize_answer: false`; the frontend helper does this for explicit V39 requests,
leaving earlier defaults unchanged. No fourth outline call, runtime guard change
or production promotion. Frozen regression/correction/fresh cases and matching
desktop checks remain required before private pilot readiness.
