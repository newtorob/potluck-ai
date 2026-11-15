# Shared source text in read-only review packets

`apps/cli/src/agentReviewPacket.ts` optionally packages an exact source block
that also occurs once in a candidate. The packet stores the source once and the
candidate as `prefix + source + suffix`. It does not summarize or remove text.

```ts
import { packSharedReviewText, restoreSharedReviewText } from './agentReviewPacket.js';

const { packet, binding } = packSharedReviewText(approvedSourceText, actualCandidateText);
const restored = restoreSharedReviewText(packet, binding);
// Send the packet as supplied data; keep the binding with the host receipt.
```

The host must restore and compare both original strings before dispatch. SHA-256
bindings detect changes to either text. Source text is capped at 16 KiB and the
candidate at 32 KiB in UTF-8. Missing or repeated source blocks, invalid Unicode,
NULs, unknown fields or versions, and corrupt bindings are rejected. A candidate
without one exact whole source block needs a different representation; this
helper never infers semantic equivalence or silently falls back to a partial
source.

A reviewer consuming this optional representation needs its layout stated in the
assignment: `candidate.shared` refers to the complete `source` value, between the
literal prefix and suffix. The packet remains untrusted evidence, not instructions
or authority. Existing role protocols, output validation, scope, model identity,
input budgets and source approvals remain unchanged. Measure the complete role
prompt before inference; a smaller serialized packet is not a token-budget proof.

Round-trip validity establishes text identity only. Primary review must still
judge relevance, conditions, missing facts and the actual model result. Do not
count successful packing, valid JSON, or a clean reviewer response as answer
correctness or owner acceptance.
