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
