# Next build boundary

The deterministic calculator is not the limiting factor by itself. Keep v8 internal
while addressing planning, extraction coverage and visible evidence together.

1. Preserve meaningful request text and expose the proposed operation plan for
   review before generation. Require one result per confirmed item; show omitted,
   duplicated or unsupported items explicitly. Keep the original issue visible so
   the owner can verify the plan. Never equate a valid schema with complete intent.
2. Distinguish "not extracted by this calculator" from "not present in the source."
   Return extraction limitations as structured data. Add general, bounded handling
   for ordinary count-unit phrases and spelled numbers, with exact source spans
   and adversarial ambiguity tests. Do not patch only the newly observed nouns.
3. Require visible evidence for stated limitations and competing values. The
   source that establishes missing authority or a denominator should appear with
   the answer. An unavailable calculation must not hide the values already known.
4. Develop with the consumed cases, then freeze code and a newly authored test
   corpus before a single paired evaluation. Retain errors, incomplete answers,
   both reviewer assessments and any disagreements.
5. After a candidate clears those checks, have Rob review real Potluck
   investigations and measure corrections and time to accepted output. Only then
   consider preview exposure and a capability announcement.

This is a proposed follow-up, not implemented behavior or an owner acceptance result.
